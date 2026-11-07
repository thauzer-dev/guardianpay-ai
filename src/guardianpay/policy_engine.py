"""Policy Enforcement Layer determinístico. Nenhuma ação passa sem ele; o LLM não participa.

Ordem das verificações (falha na primeira -> denied):
 1. ferramenta existe no catálogo            5. evidências obrigatórias presentes
 2. ator conhecido, ativo                     6. regras específicas (fornecedor/título bloqueado, lote aberto…)
 3. papel do ator permitido para a ferramenta 7. alçada (com agregação anti-fracionamento) / aprovação dupla
 4. parâmetros obrigatórios e desconhecidos   8. se exige aprovação: só executa com solicitação aprovada válida
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum

from .identity import Actor, ActorRegistry, UnknownActor
from .policies import PolicySet
from .tools.catalog import ToolKind, ToolSpec
from .tools.contract import ToolRequest

APPROVER_ONLY_TOOLS = {"aprovar_acao", "recusar_acao", "aprovar_alteracao_bancaria", "liberar_lote"}
DUAL_ALWAYS = {"reverter_alteracao_cadastro", "aprovar_alteracao_bancaria"}
EVIDENCE_PARAMS = {"evidencias"}
SENSITIVE_FIELDS = {"fornecedor_id", "forma_pagamento", "linha_digitavel"}  # corrigir_lancamento: exige aprovador


class Outcome(str, Enum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


@dataclass
class Decision:
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    required: list[dict] = field(default_factory=list)   # [{'role':'aprovador'}, {'pessoa_id':'PS01'}]
    amount_cents: int = 0


class PolicyEngine:
    def __init__(self, conn: sqlite3.Connection, catalog: dict[str, ToolSpec], policies: PolicySet,
                 actors: ActorRegistry):
        self.conn, self.catalog, self.p, self.actors = conn, catalog, policies, actors

    # ------------------------------------------------------------------ API
    def evaluate(self, req: ToolRequest, sim_ts: str) -> Decision:
        sim_date = sim_ts  # ISO completo (data+hora): regras de corte usam a hora
        spec = self.catalog.get(req.tool)
        if spec is None:
            return Decision(Outcome.deny, [f"ferramenta '{req.tool}' fora do catálogo"])
        try:
            actor = self.actors.get(req.actor_id)
        except UnknownActor:
            return Decision(Outcome.deny, [f"ator desconhecido '{req.actor_id}'"])
        if not actor.active:
            return Decision(Outcome.deny, [f"ator '{actor.actor_id}' inativo"])
        if actor.role not in spec.roles:
            return Decision(Outcome.deny, [f"papel '{actor.role}' não autorizado para '{req.tool}'"])
        if req.tool in APPROVER_ONLY_TOOLS and actor.kind == "agent":
            return Decision(Outcome.deny, ["agente não pode aprovar nem liberar"])

        missing = [p for p in spec.params_required if req.params.get(p) in (None, "", [], {})]
        if missing:
            return Decision(Outcome.deny, [f"parâmetros obrigatórios ausentes: {missing}"])
        unknown = set(req.params) - spec.all_params
        if unknown:
            return Decision(Outcome.deny, [f"parâmetros desconhecidos: {sorted(unknown)}"])
        if EVIDENCE_PARAMS & set(spec.params_required):
            ev = req.params.get("evidencias")
            if not isinstance(ev, list) or not all(isinstance(e, dict) and e.get("source") and e.get("ref") for e in ev):
                return Decision(Outcome.deny, ["evidências obrigatórias devem ser lista de {source, ref}"])

        if spec.kind is ToolKind.leitura:
            return Decision(Outcome.allow)

        specific = getattr(self, f"_rule_{req.tool}", None)
        decision = specific(req, actor, sim_date) if specific else Decision(Outcome.allow)
        if decision.outcome is Outcome.require_approval and req.approval_id:
            return self._check_approval(req, decision)
        return decision

    # ------------------------------------------------------------------ alçadas
    def _value_requirements(self, amount: int, dual: bool) -> list[dict]:
        if amount > self.p.aprovador_limite_cents:
            return [{"role": "aprovador"}, {"pessoa_id": "PS03"}] if dual else [{"pessoa_id": "PS03"}]
        if dual:
            return [{"role": "aprovador"}, {"pessoa_id": "PS01"}]
        if amount > self.p.analista_limite_cents:
            return [{"role": "aprovador"}]
        return []

    def _aggregate_same_day(self, titulo_id: str, lote: str | None) -> int:
        """Anti-fracionamento: soma títulos do mesmo fornecedor + mesma NF no mesmo dia de pagamento."""
        t = self.conn.execute("SELECT t.fornecedor_id, n.nf_num, t.valor_cents FROM op_titulos t JOIN n_titulos n USING(titulo_id) "
                              "WHERE titulo_id=?", (titulo_id,)).fetchone()
        if t is None or not t["nf_num"]:
            return t["valor_cents"] if t else 0
        day = None
        if lote:
            r = self.conn.execute("SELECT data_prevista FROM op_lotes WHERE lote_remessa=?", (lote,)).fetchone()
            day = r["data_prevista"] if r else None
        if day is None:
            return t["valor_cents"]
        row = self.conn.execute(
            "SELECT coalesce(sum(o.valor_cents),0) s FROM op_titulos o JOIN n_titulos n USING(titulo_id) "
            "JOIN op_lotes l ON l.lote_remessa=o.lote_remessa WHERE o.fornecedor_id=? AND n.nf_num=? "
            "AND l.data_prevista=? AND o.titulo_id<>? AND o.status='agendado'",
            (t["fornecedor_id"], t["nf_num"], day, titulo_id)).fetchone()
        return t["valor_cents"] + row["s"]

    def _lote_accepts_changes(self, lote_id: str, sim_ts: str) -> tuple[bool, str]:
        """Lote só aceita inclusão/retirada/bloqueio antes do horário de fechamento (relógio simulado)."""
        lote = self.conn.execute("SELECT * FROM op_lotes WHERE lote_remessa=?", (lote_id,)).fetchone()
        if lote is None:
            return False, "lote inexistente"
        if lote["status"] != "aberto":
            return False, f"lote {lote_id} já {lote['status']}: alteração retroativa proibida"
        close = f"{lote['data_prevista']}T{lote['horario_fechamento']}:00"
        if len(sim_ts) > 10 and sim_ts >= close:
            return False, f"horário de corte do lote {lote_id} ({close}) já passou"
        return True, ""

    def _missing_documents(self, titulo_id: str) -> list[str]:
        """Pagamento sem NF (exceto aluguel) ou sem OC aprovada (exceto utilidades) => aprovação dupla."""
        r = self.conn.execute("SELECT o.oc_id, n.nf_num, n.categoria, c.status AS oc_status FROM op_titulos o JOIN n_titulos n "
                              "USING(titulo_id) LEFT JOIN n_oc c ON c.oc_id=o.oc_id WHERE o.titulo_id=?", (titulo_id,)).fetchone()
        if r is None:
            return []
        out = []
        disp = self.p.dispensas()
        if r["categoria"] not in disp["nf"] and not r["nf_num"]:
            out.append("pagamento sem NF")
        if r["categoria"] not in disp["oc"] and (not r["oc_id"] or r["oc_status"] not in ("aprovada", "encerrada")):
            out.append("pagamento sem OC aprovada")
        return out

    def _titulo(self, titulo_id: str):
        return self.conn.execute("SELECT * FROM op_titulos WHERE titulo_id=?", (titulo_id,)).fetchone()

    def _recent_unconfirmed_bank_change(self, fornecedor_id: str, sim_date: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM raw_alteracoes_cadastro WHERE fornecedor_id=? AND campo IN ('banco','agencia','conta','chave_pix') "
            "AND confirmacao_registrada<>'sim' AND julianday(?) - julianday(substr(data_hora,1,10)) < 30",
            (fornecedor_id, sim_date)).fetchone()
        return row is not None

    # ------------------------------------------------------------------ regras por ferramenta
    def _rule_agendar_titulo(self, req, actor, sim_date):
        t = self._titulo(req.params["titulo_id"])
        if t is None:
            return Decision(Outcome.deny, ["título inexistente"])
        if t["status"] in ("pago", "cancelado"):
            return Decision(Outcome.deny, [f"título com status '{t['status']}' não pode ser agendado"])
        if t["bloqueado"]:
            return Decision(Outcome.deny, ["título bloqueado pela auditoria; só o aprovador desbloqueia"])
        f = self.conn.execute("SELECT bloqueado FROM op_fornecedores WHERE fornecedor_id=?", (t["fornecedor_id"],)).fetchone()
        if f and f["bloqueado"]:
            return Decision(Outcome.deny, ["fornecedor bloqueado"])
        ok, why = self._lote_accepts_changes(req.params["lote_remessa"], sim_date)
        if not ok:
            return Decision(Outcome.deny, [why])
        lote = self.conn.execute("SELECT * FROM op_lotes WHERE lote_remessa=?", (req.params["lote_remessa"],)).fetchone()
        amount = self._aggregate_same_day(t["titulo_id"], lote["lote_remessa"])
        missing_docs = self._missing_documents(t["titulo_id"])
        dual = (lote["data_prevista"] < t["vencimento"] or self._recent_unconfirmed_bank_change(t["fornecedor_id"], sim_date)
                or bool(missing_docs))
        req_ = self._value_requirements(amount, dual)
        reasons = [f"{m}: aprovação dupla" for m in missing_docs]
        if lote["data_prevista"] < t["vencimento"]:
            reasons.append("antecipação exige aprovação dupla")
        if amount > t["valor_cents"]:
            reasons.append(f"alçada avaliada pela soma do fornecedor+NF no dia: {amount / 100:.2f}")
        return Decision(Outcome.require_approval if req_ else Outcome.allow, reasons, req_, amount)

    def _rule_cancelar_titulo(self, req, actor, sim_date):
        t = self._titulo(req.params["titulo_id"])
        if t is None or t["status"] in ("pago", "cancelado"):
            return Decision(Outcome.deny, ["título inexistente, pago ou já cancelado"])
        if t["status"] == "agendado":
            ok, why = self._lote_accepts_changes(t["lote_remessa"], sim_date)
            if not ok:
                return Decision(Outcome.deny, [why])
        req_ = self._value_requirements(t["valor_cents"], False)
        return Decision(Outcome.require_approval if req_ else Outcome.allow, [], req_, t["valor_cents"])

    def _rule_corrigir_lancamento(self, req, actor, sim_date):
        t = self._titulo(req.params["titulo_id"])
        if t is None or t["status"] in ("pago", "cancelado"):
            return Decision(Outcome.deny, ["título inexistente, pago ou cancelado"])
        campo = req.params["campo"]
        if campo not in ("valor", "numero_nf", "vencimento") and campo not in SENSITIVE_FIELDS:
            return Decision(Outcome.deny, [f"campo '{campo}' não corrigível por esta ferramenta"])
        if t["status"] == "agendado":
            ok, why = self._lote_accepts_changes(t["lote_remessa"], sim_date)
            if not ok:
                return Decision(Outcome.deny, [why])
        if campo in SENSITIVE_FIELDS:
            return Decision(Outcome.require_approval, [f"trocar '{campo}' exige aprovador"], [{"role": "aprovador"}], t["valor_cents"])
        if campo == "valor":
            from .normalize import to_cents
            diff = abs(to_cents(req.params["valor_novo"]) - t["valor_cents"])
            req_ = [] if diff <= self.p.analista_limite_cents else self._value_requirements(diff, False)
            return Decision(Outcome.require_approval if req_ else Outcome.allow, [], req_, diff)
        return Decision(Outcome.allow)

    def _rule_retirar_do_lote(self, req, actor, sim_date):
        t = self._titulo(req.params["titulo_id"])
        if t is None:
            return Decision(Outcome.deny, ["título inexistente"])
        if t["status"] != "agendado" or not t["lote_remessa"]:
            return Decision(Outcome.deny, ["título não está agendado em lote"])
        ok, why = self._lote_accepts_changes(t["lote_remessa"], sim_date)
        if not ok:
            return Decision(Outcome.deny, [why])
        return Decision(Outcome.allow)  # reversível: permitido sem aprovação (ferramentas.csv)

    def _rule_bloquear_titulo(self, req, actor, sim_date):
        t = self._titulo(req.params["titulo_id"])
        if t is None or t["status"] in ("pago", "cancelado"):
            return Decision(Outcome.deny, ["título inexistente, pago ou cancelado: bloqueio sem efeito"])
        if t["bloqueado"]:
            return Decision(Outcome.deny, ["título já bloqueado"])
        if t["status"] == "agendado" and t["lote_remessa"]:
            ok, why = self._lote_accepts_changes(t["lote_remessa"], sim_date)
            if not ok:
                return Decision(Outcome.deny, [why])
        return Decision(Outcome.allow)  # contenção reversível (desbloqueio pelo aprovador)

    def _rule_reverter_alteracao_cadastro(self, req, actor, sim_date):
        return Decision(Outcome.require_approval, ["reversão de cadastro exige aprovação dupla"],
                        self._value_requirements(0, True))

    def _rule_solicitar_aprovacao(self, req, actor, sim_date):
        if req.params["acao"] not in self.catalog or req.params["acao"] == "solicitar_aprovacao":
            return Decision(Outcome.deny, ["ação solicitada fora do catálogo"])
        if not isinstance(req.params["parametros"], dict):
            return Decision(Outcome.deny, ["parametros deve ser objeto"])
        return Decision(Outcome.allow)

    def _daily_limit(self, tool: str, kind: str, sim_date: str) -> Decision:
        limit = self.catalog[tool].limits.get("por_dia")
        if limit is None:
            return Decision(Outcome.allow)
        n = self.conn.execute("SELECT count(*) FROM outbox WHERE kind=? AND substr(sim_ts,1,10)=?", (kind, sim_date[:10])).fetchone()[0]
        if n >= int(limit):
            return Decision(Outcome.deny, [f"limite diário de {limit} atingido para {tool}"])
        return Decision(Outcome.allow)

    def _rule_acionar_pessoa(self, req, actor, sim_date):
        return self._daily_limit("acionar_pessoa", "acionamento", sim_date)

    def _rule_contatar_fornecedor(self, req, actor, sim_date):
        return self._daily_limit("contatar_fornecedor", "fornecedor", sim_date)

    def _rule_aprovar_acao(self, req, actor, sim_date):
        sol = self.conn.execute("SELECT * FROM approval_requests WHERE solicitacao_id=?", (req.params["solicitacao_id"],)).fetchone()
        if sol is None or sol["status"] != "pending":
            return Decision(Outcome.deny, ["solicitação inexistente ou não pendente"])
        if sol["requested_by"] == actor.actor_id:
            return Decision(Outcome.deny, ["segregação: quem solicita não aprova a própria solicitação"])
        approvals = json.loads(sol["approvals"])
        if any(a["actor_id"] == actor.actor_id or (actor.pessoa_id and a.get("pessoa_id") == actor.pessoa_id) for a in approvals):
            return Decision(Outcome.deny, ["mesma pessoa não pode aprovar duas vezes"])
        params = json.loads(sol["params"])
        amount = int(params.get("_amount_cents", 0))
        required = json.loads(sol["required"])
        value_covered_by_director = any(r.get("pessoa_id") == "PS03" for r in required)
        if actor.alcada_cents is not None and amount > actor.alcada_cents and not value_covered_by_director:
            return Decision(Outcome.deny, [f"valor {amount / 100:.2f} acima da alçada do aprovador"])
        if not _assign(required, approvals + [{"actor_id": actor.actor_id, "role": actor.role, "pessoa_id": actor.pessoa_id}]):
            return Decision(Outcome.deny, ["aprovador não satisfaz nenhum requisito pendente"])
        return Decision(Outcome.allow)

    def _rule_recusar_acao(self, req, actor, sim_date):
        sol = self.conn.execute("SELECT status, requested_by FROM approval_requests WHERE solicitacao_id=?",
                                (req.params["solicitacao_id"],)).fetchone()
        if sol is None or sol["status"] != "pending":
            return Decision(Outcome.deny, ["solicitação inexistente ou não pendente"])
        if sol["requested_by"] == actor.actor_id:
            return Decision(Outcome.deny, ["segregação: solicitante não decide a própria solicitação"])
        return Decision(Outcome.allow)

    # ------------------------------------------------------------------ aprovação concedida
    def _check_approval(self, req: ToolRequest, decision: Decision) -> Decision:
        sol = self.conn.execute("SELECT * FROM approval_requests WHERE solicitacao_id=?", (req.approval_id,)).fetchone()
        if sol is None:
            return Decision(Outcome.deny, ["solicitação de aprovação inexistente"])
        if sol["status"] != "approved":
            return Decision(Outcome.deny, [f"solicitação em estado '{sol['status']}', não aprovada"])
        if sol["tool"] != req.tool:
            return Decision(Outcome.deny, ["aprovação é de outra ferramenta"])
        approved_params = {k: v for k, v in json.loads(sol["params"]).items() if not k.startswith("_")}
        if approved_params != req.params:
            return Decision(Outcome.deny, ["parâmetros diferem dos aprovados"])
        approvals = json.loads(sol["approvals"])
        if not _assign(decision.required, approvals, need_all=True):
            return Decision(Outcome.deny, ["aprovações registradas não cobrem os requisitos atuais"])
        return Decision(Outcome.allow, ["autorizado por " + req.approval_id], decision.required, decision.amount_cents)


def _assign(required: list[dict], approvals: list[dict], need_all: bool = False) -> bool:
    """Casamento requisito↔aprovador (cada aprovador cobre no máximo um requisito).

    need_all=False: verdadeiro se a última aprovação da lista cobre algum requisito ainda descoberto.
    need_all=True: verdadeiro se todos os requisitos estão cobertos.
    """
    def matches(req, a):
        return (req.get("role") and a.get("role") == req["role"]) or (req.get("pessoa_id") and a.get("pessoa_id") == req["pessoa_id"])

    def best(reqs, apps):
        # backtracking (listas pequenas): máximo de requisitos cobertos
        if not reqs:
            return 0
        r, rest = reqs[0], reqs[1:]
        top = best(rest, apps)
        for i, a in enumerate(apps):
            if matches(r, a):
                top = max(top, 1 + best(rest, apps[:i] + apps[i + 1:]))
        return top

    if need_all:
        return best(required, approvals) == len(required)
    return best(required, approvals) > best(required, approvals[:-1])
