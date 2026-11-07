"""Orchestrator Agent: OBSERVAR → TRIAR → INVESTIGAR → DECIDIR → AGIR → VERIFICAR → REGISTRAR.

* Observa eventos persistidos em ordem cronológica (sim_ts, seq); consumo idempotente (at-least-once +
  efeitos idempotentes = efetivamente uma vez). Evento que falha 3 vezes vai para dead-letter (processed=2).
* Cada caso avança no máximo uma vez por ciclo (prevenção de loop); falhas de execução têm limite de tentativas.
* Toda ação passa pelo ToolExecutor (PolicyEngine + idempotência + auditoria). Chaves determinísticas por
  caso/ação garantem que retomada após reinício nunca repete operação financeira.
* LLM (opcional) só redige a análise contextual; decisão, valores, permissões e execução são determinísticos.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .. import events
from ..auditing.context import AuditContext
from ..auditing.rules import run_rules
from ..domain.cases import CaseState as S
from ..domain.cases import open_case, transition
from ..domain.models import Finding
from ..log import get_logger, log
from ..store import transaction
from ..tools.contract import ToolRequest, ToolStatus
from .decision import Plan, plan_for

LOG = get_logger("orchestrator")
MAX_ATTEMPTS = 3
MAX_EVENT_ATTEMPTS = 3
WORKABLE = (S.detected, S.investigating, S.authorized, S.executing, S.failed, S.awaiting_approval)
ORCH = "agent:orchestrator"


class Orchestrator:
    def __init__(self, rt, llm=None):
        self.rt, self.conn, self.llm = rt, rt.conn, llm
        self.crash_after_actions: int | None = None   # injeção de falha para testes de retomada

    @property
    def now(self) -> str:
        return self.rt.clock.now.isoformat()

    # ================================================================ ciclo
    def cycle(self) -> dict:
        stats = {"eventos": self.process_events(), **self.work_cases()}
        return stats

    # ================================================================ OBSERVAR
    def process_events(self) -> int:
        n = 0
        need_sweep = False
        for ev in events.pending(self.conn, self.now):
            try:
                if ev["type"] in (events.EventType.snapshot_carregado.value, events.EventType.titulo_lancado.value):
                    need_sweep = True
                elif ev["type"] == events.EventType.comunicacao_recebida.value and ev["sim_ts"] > self.rt.settings.sim_start.isoformat():
                    need_sweep = True   # comunicação nova durante a simulação pode criar/agravar casos
                elif ev["type"] == "lote.fechado":
                    self._reconcile_closed_lot(json.loads(ev["payload"]))
                with transaction(self.conn):
                    events.mark_processed(self.conn, ev["event_id"])
                n += 1
            except Exception as e:  # noqa: BLE001
                with transaction(self.conn):
                    self.conn.execute("UPDATE events SET attempts=attempts+1, last_error=?, processed=CASE WHEN attempts+1>=? THEN 2 ELSE 0 END "
                                      "WHERE event_id=?", (str(e)[:300], MAX_EVENT_ATTEMPTS, ev["event_id"]))
                log(LOG, "falha ao processar evento", event=ev["event_id"], error=str(e))
        if need_sweep:
            self.sweep()
        return n

    # ================================================================ TRIAR
    def sweep(self) -> int:
        """Roda as regras de auditoria e abre casos (idempotente por case_id)."""
        ctx = AuditContext(self.conn, self.now)
        findings = run_rules(ctx)
        created = 0
        with transaction(self.conn):
            for f in findings:
                if open_case(self.conn, f, self.now, "agent:financial_audit"):
                    created += 1
                    self.conn.execute("UPDATE cases SET priority=? WHERE case_id=?", (self._priority(f, ctx), f.case_id))
        log(LOG, "varredura de auditoria", findings=len(findings), novos_casos=created)
        return created

    @staticmethod
    def _priority(f: Finding, ctx: AuditContext) -> float:
        """Heurística de ordenação (NÃO é probabilidade): exposição × urgência do corte × força da evidência."""
        hours = 999.0
        if f.lote_remessa and ctx.lote_close_ts(f.lote_remessa):
            hours = max((datetime.fromisoformat(ctx.lote_close_ts(f.lote_remessa)) - datetime.fromisoformat(ctx.sim_ts)).total_seconds() / 3600, 1)
        weight = {"forte": 1.0, "moderada": 0.5, "fraca": 0.1}[f.strength.value]
        return round(f.exposure_cents / 100 * weight / hours, 2)

    # ================================================================ INVESTIGAR / DECIDIR / AGIR / VERIFICAR
    def work_on(self, case_ids: list[str]) -> dict:
        """Trabalha SOMENTE os casos indicados (pedido do colaborador sobre um título), pelo mesmo caminho do ciclo."""
        return self.work_cases(only=set(case_ids)) if case_ids else {"casos_trabalhados": 0, "acoes": 0}

    def work_cases(self, only: set[str] | None = None) -> dict:
        rows = self.conn.execute(
            f"SELECT * FROM cases WHERE state IN ({','.join('?' * len(WORKABLE))}) AND (next_check_sim_ts IS NULL OR next_check_sim_ts<=?) "
            "ORDER BY priority DESC, case_id", (*[s.value for s in WORKABLE], self.now)).fetchall()
        if only is not None:
            rows = [r for r in rows if r["case_id"] in only]
        stats = {"casos_trabalhados": 0, "acoes": 0}
        if not rows:
            return stats
        ctx = _LazyContext(self.conn, self.now)   # só materializa se algum caso precisar (perf: Entrega 2.1)
        self._fresh = None   # re-validação calculada uma vez por ciclo (sob demanda)
        self._approval_status = dict(self.conn.execute("SELECT solicitacao_id, status FROM approval_requests").fetchall())
        for row in rows:   # cada caso avança no máximo uma vez por ciclo
            try:
                stats["acoes"] += self._step(row, ctx)
                stats["casos_trabalhados"] += 1
            except _SimulatedCrash:
                raise
            except Exception as e:  # noqa: BLE001
                with transaction(self.conn):
                    self.conn.execute("UPDATE cases SET last_error=? WHERE case_id=?", (f"{type(e).__name__}: {e}"[:300], row["case_id"]))
                log(LOG, "erro ao trabalhar caso", case=row["case_id"], error=str(e))
        return stats

    def _titulos_state(self, f: Finding) -> dict[str, dict]:
        if not f.titulos:
            return {}
        q = ",".join("?" * len(f.titulos))
        return {r["titulo_id"]: dict(r) for r in self.conn.execute(f"SELECT * FROM op_titulos WHERE titulo_id IN ({q})", f.titulos)}

    def _step(self, row: sqlite3.Row, ctx: AuditContext) -> int:
        cid, state = row["case_id"], S(row["state"])
        f = Finding.model_validate_json(row["body"])

        if state is S.detected:
            with transaction(self.conn):
                transition(self.conn, cid, S.investigating, sim_ts=self.now, actor=ORCH, reason="triagem: caso aceito para investigação")
            state = S.investigating

        if state is S.investigating:
            # re-validação: a condição ainda existe no estado atual? (evita agir sobre achado obsoleto)
            if self._fresh is None:
                self._fresh = {x.case_id: x for x in run_rules(ctx)}
            fresh = self._fresh
            if f.case_id not in fresh:
                with transaction(self.conn):
                    transition(self.conn, cid, S.resolved, sim_ts=self.now, actor=ORCH,
                               reason="re-validação: condição não existe mais no estado operacional")
                    self._record(cid, "descartar", "Condição detectada não se confirma no estado atual.", [], [])
                return 0
            f = fresh[f.case_id]
            plan = plan_for(f, self._titulos_state(f), ctx.lote_open, lambda root: (ctx.forn_by_root.get(root) or [{}])[0].get("fornecedor_id"))
            llm_note = "nao_usado"
            if self.llm is not None:   # raciocínio contextual opcional; guarda determinística no reasoner + PolicyEngine
                from .reasoner import ContextualReasoner
                if getattr(self, "_reasoner", None) is None or self._reasoner.llm is not self.llm:
                    self._reasoner = ContextualReasoner(self.rt, self.llm)
                out = self._reasoner.refine(f, plan, cid)
                plan, llm_note = out.plan, out.note
            if not plan.actions:
                with transaction(self.conn):
                    self._record(cid, plan.decision, plan.rationale, [], plan.policy_refs, llm_note)
                    if plan.target_state == "resolved":
                        transition(self.conn, cid, S.resolved, sim_ts=self.now, actor=ORCH, reason=plan.decision)
                    elif plan.target_state == "escalated":
                        transition(self.conn, cid, S.escalated, sim_ts=self.now, actor=ORCH, reason=plan.decision)
                    else:   # monitorar: reavalia só no próximo dia útil 07h (sem loop por tique)
                        self.conn.execute("UPDATE cases SET next_check_sim_ts=? WHERE case_id=?", (self._next_morning(), cid))
                return 0
            # pré-validação de política (dry-run): ação proibida => não executa nada, escala com o motivo
            denied = []
            for a in plan.actions:
                d = self.rt.engine.evaluate(ToolRequest(tool=a.tool, actor_id=a.actor_id, params=a.params), self.now)
                if d.outcome.value == "deny":
                    denied.append(f"{a.tool}: {'; '.join(d.reasons)}")
            plan.actions = [a for a in plan.actions if not any(x.startswith(a.tool + ":") for x in denied)] if denied else plan.actions
            with transaction(self.conn):
                self._record(cid, plan.decision, plan.rationale + (f" Ações negadas pela política: {denied}" if denied else ""),
                             [a.__dict__ for a in plan.actions], plan.policy_refs, llm_note)
                self.conn.execute("UPDATE cases SET pending_request=? WHERE case_id=?",
                                  (json.dumps({"target": plan.target_state, "actions": [a.__dict__ for a in plan.actions]}), cid))
                if not plan.actions:
                    transition(self.conn, cid, S.escalated, sim_ts=self.now, actor=ORCH, reason=f"nenhuma ação permitida: {denied}")
                    return 0
                transition(self.conn, cid, S.authorized, sim_ts=self.now, actor=ORCH,
                           reason=f"plano '{plan.decision}' aprovado pelo motor de políticas ({len(plan.actions)} ações)")
            state = S.authorized

        if state in (S.authorized, S.failed, S.executing):
            return self._execute_pending(row["case_id"], state)

        if state is S.awaiting_approval:
            return self._follow_up_approvals(cid, f, ctx, row["pending_request"])
        return 0

    # ---------------------------------------------------------------- AGIR + VERIFICAR
    def _execute_pending(self, cid: str, state: S) -> int:
        row = self.conn.execute("SELECT * FROM cases WHERE case_id=?", (cid,)).fetchone()
        pend = json.loads(row["pending_request"] or "{}")
        actions = pend.get("actions", [])
        if state is S.failed:
            if row["attempts"] >= MAX_ATTEMPTS:
                with transaction(self.conn):
                    transition(self.conn, cid, S.escalated, sim_ts=self.now, actor=ORCH, reason=f"limite de {MAX_ATTEMPTS} tentativas atingido")
                    self._record(cid, "escalar", f"Falhas repetidas: {row['last_error']}", [], [])
                return 0
        if state in (S.authorized, S.failed):
            with transaction(self.conn):
                transition(self.conn, cid, S.executing, sim_ts=self.now, actor=ORCH, reason="executando plano" if state is S.authorized else "nova tentativa (idempotente)")
        executed, failures, pending_ids, statuses = 0, [], [], {}
        for i, a in enumerate(actions):
            res = self.rt.executor.execute(ToolRequest(tool=a["tool"], actor_id=a["actor_id"], params=a["params"],
                                                       idempotency_key=f"{cid}:{a['key']}", case_id=cid))
            executed += 0 if res.replayed else 1
            statuses[a["key"]] = res.status
            if res.status is ToolStatus.ok:
                pass
            elif res.status is ToolStatus.pending_approval:
                pending_ids.append({"solicitacao_id": res.solicitacao_id, "action": a})
            elif res.status is ToolStatus.denied and a["purpose"] in ("notify", "incident"):
                pass  # limite diário etc.: não bloqueia a contenção
            else:
                failures.append(f"{a['tool']}: {res.status.value} {res.reasons}")
            if a["tool"] == "solicitar_aprovacao" and res.status is ToolStatus.ok:
                pending_ids.append({"solicitacao_id": res.data["solicitacao_id"], "action": {
                    "tool": a["params"]["acao"], "actor_id": a["actor_id"], "params": a["params"]["parametros"],
                    "purpose": "approved_execution", "key": f"exec:{a['key']}"}})
            if self.crash_after_actions is not None and i + 1 >= self.crash_after_actions:
                raise _SimulatedCrash()
        ok_verify, vmsg = self._verify(actions, statuses)
        with transaction(self.conn):
            if failures or not ok_verify:
                self.conn.execute("UPDATE cases SET attempts=attempts+1, last_error=? WHERE case_id=?",
                                  ("; ".join(failures) or vmsg, cid))
                transition(self.conn, cid, S.failed, sim_ts=self.now, actor=ORCH, reason=("; ".join(failures) or vmsg)[:300])
                return executed
            target = S(pend.get("target", "escalated"))
            if pending_ids and target is S.resolved:
                target = S.awaiting_approval
            if target is S.awaiting_approval and not pending_ids:
                target = S.escalated   # contenção feita, decisão final é humana (sem solicitação formal pendente)
            self.conn.execute("UPDATE cases SET pending_request=? WHERE case_id=?",
                              (json.dumps({"target": target.value, "actions": actions, "approvals": pending_ids,
                                           "versions": self._versions(json.loads(row["titulos"]))}), cid))
            transition(self.conn, cid, target, sim_ts=self.now, actor=ORCH, verified=True,
                       reason=f"ações verificadas no estado operacional ({vmsg}); " +
                              ("pendência acompanhada, não resolvida" if target is not S.resolved else "causa corrigida"))
        return executed

    def _verify(self, actions: list[dict], statuses: dict) -> tuple[bool, str]:
        """Lê de volta o estado operacional (não confia só no retorno da ferramenta)."""
        checks = []
        for a in actions:
            p = a["params"]
            if statuses.get(a["key"]) is not ToolStatus.ok:
                continue   # pendente de aprovação / negada: nada executado a verificar
            if a["tool"] in ("retirar_do_lote", "bloquear_titulo", "corrigir_lancamento"):
                t = self.conn.execute("SELECT * FROM op_titulos WHERE titulo_id=?", (p["titulo_id"],)).fetchone()
                if a["tool"] == "retirar_do_lote":
                    ok = t["status"] != "agendado" or t["lote_remessa"] == ""
                elif a["tool"] == "bloquear_titulo":
                    ok = t["bloqueado"] == 1
                else:
                    if p["campo"] == "valor":
                        from ..normalize import to_cents
                        ok = t["valor_cents"] == to_cents(p["valor_novo"])
                    else:
                        ok = True
                checks.append((a["tool"], p["titulo_id"], ok))
        bad = [c for c in checks if not c[2]]
        return (not bad, f"{len(checks) - len(bad)}/{len(checks)} verificações ok" + (f"; falhou {bad}" if bad else ""))

    # ---------------------------------------------------------------- ACOMPANHAR aprovações
    def _follow_up_approvals(self, cid: str, f: Finding, ctx, pending_json: str | None = None) -> int:
        if pending_json is None:
            pending_json = self.conn.execute("SELECT pending_request FROM cases WHERE case_id=?", (cid,)).fetchone()["pending_request"]
        pend = json.loads(pending_json or "{}")
        sols = pend.get("approvals", [])
        if not sols:
            return 0
        cache = getattr(self, "_approval_status", {})
        statuses = {s["solicitacao_id"]: cache.get(s["solicitacao_id"]) or self.conn.execute(
            "SELECT status FROM approval_requests WHERE solicitacao_id=?", (s["solicitacao_id"],)).fetchone()["status"] for s in sols}
        if any(v == "rejected" for v in statuses.values()):
            with transaction(self.conn):
                transition(self.conn, cid, S.escalated, sim_ts=self.now, actor=ORCH, reason="aprovação recusada: decisão humana registrada")
                self._record(cid, "escalar", "Solicitação recusada pelo aprovador; título permanece contido até decisão humana.", [], [])
            return 0
        ready = [s for s in sols if statuses[s["solicitacao_id"]] == "approved"]
        if not ready:
            return 0   # segue aguardando; prazo de corte é acompanhado pelo agendador de lotes
        ok, checks = self._revalidate(f, ready, pend)
        with transaction(self.conn):
            self._record(cid, "revalidacao_ok" if ok else "revalidacao_falhou",
                         "Revalidação antes de executar ação aprovada: " + "; ".join(checks), [s["action"] for s in ready],
                         ["alcadas.segregacao", "seguranca"])
            if not ok:
                transition(self.conn, cid, S.escalated, sim_ts=self.now, actor=ORCH,
                           reason="aprovação recebida, mas contexto/estado/permissão mudou: não executado")
                return 0
        n = 0
        with transaction(self.conn):
            transition(self.conn, cid, S.authorized, sim_ts=self.now, actor=ORCH, reason=f"{len(ready)} aprovação(ões) concedida(s)")
            transition(self.conn, cid, S.executing, sim_ts=self.now, actor=ORCH, reason="executando ações aprovadas")
        failures = []
        for s in ready:
            a = s["action"]
            if a["tool"] == "solicitar_aprovacao":
                continue
            res = self.rt.executor.execute(ToolRequest(tool=a["tool"], actor_id=a["actor_id"], params=a["params"],
                                                       idempotency_key=f"{cid}:{a['key']}", approval_id=s["solicitacao_id"], case_id=cid))
            n += 0 if res.replayed else 1
            if res.status is not ToolStatus.ok:
                failures.append(f"{a['tool']}: {res.status.value} {res.reasons}")
        remaining = [s for s in sols if statuses[s["solicitacao_id"]] == "pending"]
        # correção aprovada de título retido: devolve ao lote original se ainda aberto (evita atraso de pagamento legítimo)
        if not failures and any(s["action"]["tool"] == "corrigir_lancamento" for s in ready):
            for tid in f.titulos[:1]:
                t = self.conn.execute("SELECT * FROM op_titulos WHERE titulo_id=?", (tid,)).fetchone()
                if t and t["status"] == "aberto" and not t["bloqueado"] and f.lote_remessa and ctx.lote_open(f.lote_remessa):
                    act = {"tool": "agendar_titulo", "actor_id": ORCH, "params": {"titulo_id": tid, "lote_remessa": f.lote_remessa},
                           "purpose": "reschedule", "key": f"reagendar:{tid}:{f.lote_remessa}"}
                    res = self.rt.executor.execute(ToolRequest(tool="agendar_titulo", actor_id=ORCH, params=act["params"],
                                                               idempotency_key=f"{cid}:{act['key']}", case_id=cid))
                    n += 0 if res.replayed else 1
                    if res.status is ToolStatus.pending_approval:
                        remaining.append({"solicitacao_id": res.solicitacao_id, "action": act})
                    elif res.status is not ToolStatus.ok:
                        failures.append(f"agendar_titulo: {res.status.value} {res.reasons}")
        with transaction(self.conn):
            if failures:
                self.conn.execute("UPDATE cases SET attempts=attempts+1, last_error=? WHERE case_id=?", ("; ".join(failures), cid))
                transition(self.conn, cid, S.failed, sim_ts=self.now, actor=ORCH, reason="; ".join(failures)[:300])
                return n
            pend["approvals"] = remaining
            pend["versions"] = self._versions(f.titulos)   # nova linha de base após as ações do próprio caso
            self.conn.execute("UPDATE cases SET pending_request=? WHERE case_id=?", (json.dumps(pend), cid))
            held = [t for t in f.titulos if (r := self.conn.execute("SELECT bloqueado FROM op_titulos WHERE titulo_id=?", (t,)).fetchone())
                    and r["bloqueado"]]
            final = S.awaiting_approval if remaining else (S.escalated if held else S.resolved)
            transition(self.conn, cid, final, sim_ts=self.now, actor=ORCH, verified=True,
                       reason="ações aprovadas executadas e verificadas" + ("; restam aprovações" if remaining else "")
                       + (f"; {len(held)} título(s) seguem bloqueados: desbloqueio é do aprovador (sem ferramenta no catálogo)" if held and not remaining else ""))
        return n

    def _versions(self, titulos: list[str]) -> dict:
        if not titulos:
            return {}
        q = ",".join("?" * len(titulos))
        return {r["titulo_id"]: r["version"] for r in self.conn.execute(f"SELECT titulo_id, version FROM op_titulos WHERE titulo_id IN ({q})", titulos)}

    def _revalidate(self, f: Finding, ready: list[dict], pend: dict) -> tuple[bool, list[str]]:
        """Antes de executar o que foi aprovado: estado não mudou por terceiros, título ainda elegível, aprovadores
        ativos e o motor de políticas (com a aprovação) ainda permite."""
        checks, ok = [], True
        before, now_v = pend.get("versions") or {}, self._versions(list((pend.get("versions") or {}).keys()))
        changed = [t for t, v in before.items() if now_v.get(t) != v]
        if changed:
            ok = False
            checks.append(f"títulos alterados por terceiros desde a solicitação: {changed}")
        else:
            checks.append("estado dos títulos inalterado desde a solicitação")
        for s in ready:
            a = s["action"]
            tid = a["params"].get("titulo_id")
            if tid:
                st = self.conn.execute("SELECT status FROM op_titulos WHERE titulo_id=?", (tid,)).fetchone()
                if st is None or st["status"] in ("pago", "cancelado"):
                    ok = False
                    checks.append(f"{tid} não está mais elegível ({st['status'] if st else 'inexistente'})")
            sol = self.conn.execute("SELECT approvals FROM approval_requests WHERE solicitacao_id=?", (s["solicitacao_id"],)).fetchone()
            inactive = [x["actor_id"] for x in json.loads(sol["approvals"]) if not self.rt.actors.get(x["actor_id"]).active]
            if inactive:
                ok = False
                checks.append(f"aprovador inativo: {inactive}")
            d = self.rt.engine.evaluate(ToolRequest(tool=a["tool"], actor_id=a["actor_id"], params=a["params"],
                                                    approval_id=s["solicitacao_id"]), self.now)
            if d.outcome.value != "allow":
                ok = False
                checks.append(f"{a['tool']}: motor de políticas {d.outcome.value} {d.reasons}")
            else:
                checks.append(f"{a['tool']}: permitido pelo motor com {s['solicitacao_id']}")
        return ok, checks

    # ---------------------------------------------------------------- REGISTRAR
    def _record(self, cid: str, decision: str, rationale: str, actions: list, refs: list, llm: str = "nao_usado") -> None:
        self.conn.execute("INSERT INTO case_decisions(case_id,sim_ts,decision,rationale,actions,policy_refs,llm) VALUES(?,?,?,?,?,?,?)",
                          (cid, self.now, decision, rationale, json.dumps(actions, ensure_ascii=False, default=str),
                           json.dumps(refs), llm))

    def _next_morning(self) -> str:
        from datetime import timedelta
        nxt = self.rt.calendar.next_business_day(self.rt.clock.now.date() + timedelta(days=1))
        return f"{nxt.isoformat()}T07:00:00"

    def _reconcile_closed_lot(self, payload: dict) -> None:
        """Lote fechado: casos ainda abertos cujos títulos saíram viram perda realizada (escalada)."""
        for tid in payload.get("pagos_com_caso", []):
            for c in self.conn.execute("SELECT case_id, state, rule_id, titulos FROM cases WHERE titulos LIKE ? "
                                       "AND state NOT IN ('resolved','escalated')", (f'%"{tid}"%',)).fetchall():
                tl = json.loads(c["titulos"])
                primary = tl if c["rule_id"] == "R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA" else tl[:1]
                if tid not in primary:
                    continue   # título de referência (ex.: o original de uma duplicidade) pago legitimamente
                with transaction(self.conn):
                    if S(c["state"]) in (S.investigating, S.awaiting_approval):
                        transition(self.conn, c["case_id"], S.escalated, sim_ts=self.now, actor=ORCH,
                                   reason=f"lote fechado com {tid} pago: perda simulada registrada")


class _LazyContext:
    """Proxy que só constrói o AuditContext (≈0,25 s) no primeiro acesso. Ciclos em que os casos apenas
    aguardam aprovação não pagam esse custo."""

    def __init__(self, conn, sim_ts):
        self._args, self._ctx = (conn, sim_ts), None

    def __getattr__(self, name):
        if self._ctx is None:
            self._ctx = AuditContext(*self._args)
        return getattr(self._ctx, name)


class _SimulatedCrash(RuntimeError):
    """Usada só nos testes de retomada."""
