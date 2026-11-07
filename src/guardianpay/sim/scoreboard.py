"""Indicadores financeiros (parametros.md) com atribuição ÚNICA por título (sem dupla contagem).

Categorias separadas:
* pagamentos_processados  — fato da simulação: títulos enviados em lotes fechados (sim_payments).
* retencoes_executadas    — fato: títulos contidos por ação do agente (retirar/bloquear executados e verificados).
* exposicao_potencial     — exposição dos achados COMPROVADOS, 1 achado por título (prioridade de regra).
* economia_estimada       — parte da exposição cujo pagamento indevido não ocorreu (realizada: lote já fechou;
                            projetada: lote fecha após 09/11 08h, avaliado no estado final).
* perda_simulada          — exposição comprovada que saiu sem saneamento (correção aprovada não é perda).
* custo_atraso_estimado   — obrigação legítima retida: multa 2% + juros 1% a.m. × 7 dias + relacionamento.
* nao_verificavel         — indícios (não comprovados): sem gabarito, não entram em perda nem economia.
Placar = −(perda + atraso + acionamentos desnecessários presumidos).
"""
from __future__ import annotations

import json

from ..auditing.rules import RULE_PRIORITY
from ..domain.models import Finding

R4 = "R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA"
OWED_RULES = {R4, "R5_FORNECEDOR_DIVERGENTE", "R6_APROVACAO_DUPLA_AUSENTE", "R2_VALOR_ACIMA_DUPLICATA", "R5_BENEFICIARIO_INCOMPATIVEL"}


def _primary(f: Finding) -> list[str]:
    return list(f.titulos) if f.rule_id == R4 else list(f.titulos[:1])


def attribution(c) -> dict[str, tuple[Finding, int]]:
    """título -> (achado responsável, exposição). Um título nunca é atribuído a dois achados."""
    out: dict[str, tuple[Finding, int]] = {}
    rows = [Finding.model_validate_json(r["body"]) for r in c.execute("SELECT body FROM cases")]
    for f in sorted(rows, key=lambda f: (RULE_PRIORITY.get(f.rule_id, 99), f.case_id)):
        for tid in _primary(f):
            if tid in out:
                continue
            if f.rule_id == R4:
                r = c.execute("SELECT valor_cents FROM n_titulos WHERE titulo_id=?", (tid,)).fetchone()
                exp = r[0] if r else 0
            else:
                exp = f.exposure_cents
            out[tid] = (f, exp)
    return out


def _sanitized(c, f: Finding, tid: str) -> bool:
    """O pagamento saiu, mas a causa foi corrigida antes (ex.: valor corrigido, fornecedor corrigido, cadastro revertido)."""
    t = c.execute("SELECT * FROM op_titulos WHERE titulo_id=?", (tid,)).fetchone()
    if f.rule_id.startswith("R2_VALOR"):
        ceiling = next((int(e.value) for e in f.evidences if e.field == "teto_legitimo_cents"), None)
        return ceiling is not None and t["valor_cents"] <= ceiling
    if f.rule_id == "R5_FORNECEDOR_DIVERGENTE":
        return t["fornecedor_id"] != f.fornecedor_id
    if f.rule_id == R4:
        alt = next((e.ref for e in f.evidences if e.source == "raw_alteracoes_cadastro"), None)
        a = c.execute("SELECT campo, valor_anterior FROM raw_alteracoes_cadastro WHERE alteracao_id=?", (alt,)).fetchone() if alt else None
        if a:
            col = {"banco": "banco", "agencia": "agencia", "conta": "conta", "chave_pix": "chave_pix"}.get(a["campo"])
            cur = c.execute(f"SELECT {col} FROM op_fornecedores WHERE fornecedor_id=?", (f.fornecedor_id,)).fetchone()
            return bool(col and cur and cur[0] == a["valor_anterior"])
    return False


def compute(rt) -> dict:
    c, p = rt.conn, rt.policies
    closed = {r["lote_remessa"] for r in c.execute("SELECT lote_remessa FROM lote_closures")}
    agenda = {r["titulo_id"]: dict(r) for r in c.execute("SELECT * FROM n_agenda")}
    paid = {r["titulo_id"]: dict(r) for r in c.execute("SELECT * FROM sim_payments")}
    attr = attribution(c)

    # ---- fatos
    proc = {"titulos": len(paid), "valor_cents": sum(x["valor_cents"] for x in paid.values())}
    contained = {r["target"] for r in c.execute("SELECT target FROM audit_log WHERE decision='executed' "
                                                 "AND action IN ('retirar_do_lote','bloquear_titulo')")}
    ret_eff = [t for t in contained if t in agenda and agenda[t]["lote_remessa"] in closed and t not in paid]
    ret_pend = [t for t in contained if t in agenda and agenda[t]["lote_remessa"] not in closed]
    retencoes = {"titulos": len(contained), "em_lotes_fechados": len(ret_eff), "em_lotes_futuros": len(ret_pend),
                 "valor_em_lotes_fechados_cents": sum(agenda[t]["valor_cents"] for t in ret_eff),
                 "fora_da_agenda": len([t for t in contained if t not in agenda])}

    # ---- exposição / economia / perda (comprovados, atribuição única, só títulos da agenda do período)
    exp_total = perda = econ_real = econ_proj = sanado = 0
    perdas, nao_verif = [], 0
    for tid, (f, exp) in attr.items():
        if tid not in agenda:
            continue
        if f.certainty.value != "comprovado":
            nao_verif += exp
            continue
        if exp <= 0:
            continue
        exp_total += exp
        if tid in paid:
            if _sanitized(c, f, tid):
                sanado += exp
                econ_real += exp
            else:
                perda += exp
                perdas.append({"titulo_id": tid, "caso": f.case_id, "perda_cents": exp})
        elif agenda[tid]["lote_remessa"] in closed:
            econ_real += exp
        else:
            econ_proj += exp

    # ---- custo de atraso (obrigação legítima que não saiu no lote previsto)
    multa, juros = p.num("atraso", "multa"), p.num("atraso", "juros_ao_mes")
    atraso, retidos = 0, []
    for tid, a in agenda.items():
        if tid in paid:
            continue
        st = c.execute("SELECT o.status, o.bloqueado, n.categoria FROM op_titulos o JOIN n_titulos n USING(titulo_id) WHERE titulo_id=?",
                       (tid,)).fetchone()
        fut_ok = a["lote_remessa"] not in closed and st["status"] == "agendado" and not st["bloqueado"]
        if fut_ok or st["status"] == "cancelado":
            continue
        f = attr.get(tid, (None, 0))[0]
        legit = f is None or f.rule_id in OWED_RULES or f.certainty.value != "comprovado"
        if not legit:
            continue
        v = a["valor_cents"]
        if f is not None and f.rule_id.startswith("R2_VALOR"):
            v = next((int(e.value) for e in f.evidences if e.source == "n_duplicatas" and e.field == "valor_cents"), v)
        cost = int(v * multa + v * juros * 7 / 30) + p.custo_relacionamento_cents(st["categoria"])
        atraso += cost
        retidos.append({"titulo_id": tid, "regra": f.rule_id if f else None, "custo_cents": cost,
                        "situacao": "retido em lote fechado" if a["lote_remessa"] in closed else "retido no estado final (projeção)"})

    acion = [dict(r) for r in c.execute("SELECT * FROM outbox WHERE kind='acionamento'")]
    comprov = {t for t, (f, _) in attr.items() if f.certainty.value == "comprovado"}
    desn = [a for a in acion if json.loads(a["titulos"]) and not (set(json.loads(a["titulos"])) & comprov)]
    custo_acion = len(desn) * p.custo_acionamento_desnecessario_cents
    return {
        "fim_simulacao": rt.clock.end.isoformat(), "lotes_fechados": len(closed),
        "pagamentos_processados": proc, "retencoes_executadas": retencoes,
        "exposicao_potencial_cents": exp_total, "economia_estimada_realizada_cents": econ_real,
        "economia_estimada_projetada_cents": econ_proj, "saneado_antes_do_pagamento_cents": sanado,
        "perda_simulada_cents": perda, "custo_atraso_estimado_cents": atraso,
        "acionamentos": len(acion), "acionamentos_sem_caso_comprovado": len(desn), "custo_acionamentos_cents": custo_acion,
        "nao_verificavel_indicios_cents": nao_verif, "placar_cents": -(perda + atraso + custo_acion),
        "titulos_atribuidos": len(attr),
        # aliases (compatibilidade Entrega 2)
        "evitado_realizado_cents": econ_real, "evitado_projetado_cents": econ_proj, "custo_atraso_cents": atraso,
        "detalhe_perdas": perdas, "detalhe_retidos": retidos,
        "observacao": "sem gabarito: comprovado = demonstrado pelos registros; estimativas sem validação estatística",
    }
