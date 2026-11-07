"""Jornada guiada de demonstração: cada etapa é VERIFICADA no estado real da instância (nada é encenado).

Cenário A — Prevenção (R5-TP431754, boleto com beneficiário de outra empresa, Pigmentos Aquarela).
Cenário B — Colaboração (R2-TP431459, título digitado 10× acima da duplicata; correção exige aprovador).
"""
from __future__ import annotations

import json

from ..audit import verify_chain

A_CASE, A_TIT, A_LOTE = "R5-TP431754", "TP431754", "RM20261103-MER"
B_CASE, B_TIT = "R2-TP431459", "TP431459"
TAG = "[SIMULAÇÃO DE TESTE]"


def _one(c, sql, a=()):
    r = c.execute(sql, a).fetchone()
    return dict(r) if r else None


def _step(sid, titulo, ok, detalhe, evid=None, proximo=None, parcial=False):
    return {"id": sid, "titulo": titulo, "status": "ok" if ok else ("parcial" if parcial else "pendente"),
            "detalhe": detalhe, "evidencias": evid or [], "proximo": None if ok else proximo}


def scenario_a(rt) -> dict:
    c = rt.conn
    case = _one(c, "SELECT * FROM cases WHERE case_id=?", (A_CASE,))
    trans = [dict(r) for r in c.execute("SELECT * FROM case_transitions WHERE case_id=? ORDER BY seq", (A_CASE,))]
    dec = _one(c, "SELECT * FROM case_decisions WHERE case_id=? ORDER BY seq LIMIT 1", (A_CASE,))
    blk = _one(c, "SELECT * FROM audit_log WHERE action='bloquear_titulo' AND target=? AND decision='executed'", (A_TIT,))
    t = _one(c, "SELECT * FROM op_titulos WHERE titulo_id=?", (A_TIT,))
    orig = _one(c, "SELECT lote_remessa FROM n_agenda WHERE titulo_id=?", (A_TIT,))
    closure = _one(c, "SELECT * FROM lote_closures WHERE lote_remessa=?", (A_LOTE,))
    paid = _one(c, "SELECT * FROM sim_payments WHERE titulo_id=?", (A_TIT,))
    notif = [r["to_ref"] for r in c.execute("SELECT to_ref FROM outbox WHERE kind='acionamento' AND titulos LIKE ?", (f'%"{A_TIT}"%',))]
    chain_ok, _ = verify_chain(c)
    body = json.loads(case["body"]) if case else {}
    retained = closure and A_TIT in json.loads(closure["titulos_retidos"])
    steps = [
        _step("a1", "Detectar pagamento irregular", bool(case),
              body.get("summary", "Ainda não detectado."), [A_CASE] if case else [], "Executar 1 passo do agente (Simulação)."),
        _step("a2", "Investigar evidências", bool(case and len(body.get("evidences", [])) >= 3 and any(x["to_state"] == "investigating" for x in trans)),
              f"{len(body.get('evidences', []))} evidências de {len({e['source'] for e in body.get('evidences', [])})} fontes (ERP, DDA, cadastro, e-mail)." if case else "—",
              [A_CASE]),
        _step("a3", "Aplicar contenção autorizada", bool(blk and t and t["bloqueado"]),
              f"bloquear_titulo executado por {blk['actor']} ({blk['actor_role']}) às {blk['sim_ts']}, auditoria #{blk['seq']}." if blk else "Sem contenção ainda.",
              [f"audit#{blk['seq']}"] if blk else []),
        _step("a4", "Verificar alteração da remessa", bool(retained and not paid),
              (f"Agenda original: {A_TIT} em {orig['lote_remessa'] if orig else '?'}. " +
               (f"Lote enviado em {closure['closed_sim_ts']} com {closure['titulos_enviados']} títulos; {A_TIT} RETIDO (não saiu)." if closure
                else f"Título bloqueado na fila; o lote fecha 03/11 10:00 — avance o relógio para ver o envio sem ele.")),
              [A_LOTE], "Avançar o relógio até 03/11 10:00.", parcial=bool(t and t["bloqueado"] and not closure)),
        _step("a5", "Registrar justificativa e auditoria", bool(dec and chain_ok and notif),
              (f"Decisão '{dec['decision']}': {dec['rationale'][:220]}… Notificados: {', '.join(sorted(set(notif)))}. Cadeia de auditoria íntegra: {chain_ok}."
               if dec else "—"), [A_CASE]),
    ]
    return {"id": "A", "nome": "Prevenção — boleto com beneficiário de outra empresa", "caso": A_CASE, "titulo": A_TIT, "etapas": steps}


def scenario_b(rt) -> dict:
    c = rt.conn
    case = _one(c, "SELECT * FROM cases WHERE case_id=?", (B_CASE,))
    body = json.loads(case["body"]) if case else {}
    sol = _one(c, "SELECT * FROM approval_requests WHERE tool='corrigir_lancamento' AND case_id=?", (B_CASE,))
    routed = json.loads(sol["routed_to"] or "[]") if sol else []
    approvals = json.loads(sol["approvals"]) if sol else []
    t = _one(c, "SELECT * FROM op_titulos WHERE titulo_id=?", (B_TIT,))
    fix = _one(c, "SELECT * FROM audit_log WHERE action='corrigir_lancamento' AND target=? AND decision='executed'", (B_TIT,))
    who = routed[0]["candidatos"][0] if routed and routed[0]["candidatos"] else None
    decided = sol and sol["status"] in ("approved", "executed", "rejected")
    simulated = any(str(a.get("comentario", "")).startswith(TAG) for a in approvals)
    steps = [
        _step("b1", "Detectar divergência que exige aprovação", bool(case), body.get("summary", "Ainda não detectado."), [B_CASE] if case else [],
              "Executar 1 passo do agente (Simulação)."),
        _step("b2", "Encaminhar ao responsável correto", bool(sol and who),
              (f"Solicitação {sol['solicitacao_id']} exige {', '.join(r.get('pessoa_id') or 'papel ' + r['role'] for r in json.loads(sol['required']))}; "
               f"roteada para {who}.") if sol else "—", [sol["solicitacao_id"]] if sol else []),
        _step("b3", "Apresentar evidências", bool(sol and json.loads(sol["evidence"])),
              f"{len(json.loads(sol['evidence']))} evidências anexadas à solicitação (título × duplicata do XML)." if sol else "—"),
        _step("b4", "Receber decisão autenticada", bool(decided),
              (f"{sol['status']} por " + ", ".join(f"{a['actor_id']} ({a['role']})" for a in approvals) +
               (" — DECISÃO SIMULADA DE TESTE" if simulated else " — via sessão autenticada na interface")) if decided else "Aguardando decisão.",
              [sol["solicitacao_id"]] if sol else [], f"Entrar como {who or 'usuario_12'} (aprovador) e aprovar em Aprovações."),
        _step("b5", "Retomar e concluir a operação", bool(case and case["state"] == "resolved" and fix and t and t["valor_cents"] == 483_800),
              (f"Correção executada (auditoria #{fix['seq']}); valor atual R$ {t['valor_cents'] / 100:.2f}; status {t['status']} "
               f"{t['lote_remessa'] or ''}; caso {case['state']}.") if fix and case else f"Caso em '{case['state'] if case else '—'}'.",
              [B_CASE]),
    ]
    return {"id": "B", "nome": "Colaboração — correção de valor com aprovação humana", "caso": B_CASE, "titulo": B_TIT, "etapas": steps,
            "aprovador_sugerido": who}


def journey(rt) -> dict:
    hours_to_cut = max(0, int((__import__("datetime").datetime.fromisoformat("2026-11-03T10:00:00") - rt.clock.now).total_seconds() // 3600))
    return {"sim_now": rt.clock.now.isoformat(), "horas_ate_corte_a": hours_to_cut, "cenarios": [scenario_a(rt), scenario_b(rt)],
            "aviso": "Status calculado do estado real da instância; nenhuma etapa é encenada."}
