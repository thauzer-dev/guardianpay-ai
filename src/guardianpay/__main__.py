"""CLI: python -m guardianpay <comando>

A CLI é um console local de operação. Ações humanas passam por sessão (login simulado do usuário do ERP);
a CLI não permite escolher identidade de agente nem papel.
"""
from __future__ import annotations

import argparse
import json

from .audit import verify_chain
from .auth import execute_with_session
from .runtime import bootstrap


def _cents(d: dict) -> dict:
    return {k.replace("_cents", "_rs"): (round(v / 100, 2) if k.endswith("_cents") and isinstance(v, int) else v) for k, v in d.items()}


def _llm_task_check(st, titulo: str) -> dict:
    """Mesma chamada do Agent Workspace (prompt, max_tokens, timeout, canal interativo, validação), num banco
    TEMPORÁRIO recém-ingerido: não toca em var/ nem nas instâncias; o ciclo autônomo não roda (zero chamadas extras)."""
    import shutil
    import tempfile
    from dataclasses import replace
    from pathlib import Path
    from .api.agent_chat import AgentChat
    tmp = Path(tempfile.mkdtemp(prefix="gp-llmcheck-"))
    try:
        rt = bootstrap(replace(st, db_path=tmp / "check.db"), reingest=True)
        before = rt.conn.execute("SELECT count(*) FROM audit_log WHERE decision IN ('executed','requires_approval')").fetchone()[0]
        q = (f"Investigue o título {titulo}. Cruze os dados de contas a pagar, fornecedor e DDA, identifique inconsistências "
             "e explique quais evidências sustentam suas conclusões.")
        r = AgentChat(rt).handle("usuario_01", q)
        d = r.get("dados") or {}
        after = rt.conn.execute("SELECT count(*) FROM audit_log WHERE decision IN ('executed','requires_approval')").fetchone()[0]
        out = {"pergunta": q, "intencao": r["intent"], "modelo_ia": d.get("modelo_ia"), "ia": d.get("diagnostico_ia"),
               "ferramentas_executadas": [f"{t['id']} {t['tool']}" for t in d.get("ferramentas_executadas", []) if t["status"] == "ok"],
               "inconsistencias": [i.get("descricao") for i in d.get("inconsistencias", [])][:6],
               "avaliacao": d.get("avaliacao") or "", "ferramentas_negadas": d.get("ferramentas_negadas", []), "mutacoes_financeiras": after - before,
               "uso": rt.llm.usage.lines if rt.llm else []}
        rt.conn.close()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="guardianpay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="(re)ingere os CSVs num banco novo")
    sub.add_parser("status", help="relógio, contagens, auditoria")
    sub.add_parser("dq", help="relatório de qualidade de dados")
    r = sub.add_parser("run", help="executa o agente: ciclo atual + avanço do relógio")
    r.add_argument("--ticks", type=int, default=None, help="padrão: até o fim (168 h)")
    sub.add_parser("cases", help="fila de casos por prioridade")
    c = sub.add_parser("case", help="detalhe de um caso (achado, transições, decisões)")
    c.add_argument("case_id")
    sub.add_parser("placar", help="placar financeiro (parametros.md)")
    lc = sub.add_parser("llm-check", help="diagnóstico do AI Proxy: 1 geração mínima com AI_PROXY_MODEL (ou listagem, se vazio)")
    lc.add_argument("--tarefa", action="store_true",
                    help="também executa UMA investigação composta real (até 3 chamadas) num banco temporário, sem ciclo autônomo")
    lc.add_argument("--titulo", default="TP431754")
    sub.add_parser("aprovacoes", help="solicitações pendentes")
    a = sub.add_parser("aprovar", help="aprovar solicitação como usuário humano (sessão)")
    a.add_argument("solicitacao_id"); a.add_argument("--as", dest="user", required=True); a.add_argument("--comentario", default="")
    rc = sub.add_parser("recusar", help="recusar solicitação como usuário humano (sessão)")
    rc.add_argument("solicitacao_id"); rc.add_argument("--as", dest="user", required=True); rc.add_argument("--motivo", required=True)
    d = sub.add_parser("demo", help="[MODO DEMONSTRAÇÃO] simula respostas de aprovação de TESTE e mostra um caso")
    d.add_argument("--case", default="R2-TP431459"); d.add_argument("--ticks", type=int, default=30)
    t = sub.add_parser("tool", help="executa ferramenta como usuário humano autenticado")
    t.add_argument("name"); t.add_argument("--as", dest="user", required=True); t.add_argument("--params", default="{}")
    t.add_argument("--key"); t.add_argument("--approval")
    args = ap.parse_args(argv)

    if args.cmd == "llm-check":   # não toca no banco nem em módulos financeiros
        from .config import load_settings
        from .llm.proxy_client import USER_AGENT, ProxyClient
        st = load_settings()
        diag = {"chave_configurada": bool(st.ai_proxy_api_key), "base_url": st.ai_proxy_base_url,
                "modelo_configurado": (st.ai_proxy_model or "").strip() or None, "modo": st.llm_mode, "user_agent": USER_AGENT}
        if not st.llm_enabled:
            diag["resultado"] = "LLM desligado (sem AI_PROXY_API_KEY ou GP_LLM_MODE=off)"
        else:
            import os
            from .llm.budget import LLMLimits
            lim = LLMLimits.from_env(os.environ)
            cli = ProxyClient(st, None, timeout=20, max_retries=0, breaker_threshold=1, max_calls=1)
            # mesma rota/modelo/formato; o que muda é o tamanho: por isso o probe passa e a investigação pode exceder o timeout
            diag["comparacao"] = {
                "probe": {"endpoint": "/chat/completions", "max_tokens": 16, "timeout_s": 20, "contexto": "~20 tokens", "reenvios": 0},
                "agent_workspace": {"endpoint": "/chat/completions", "max_tokens": min(1800, lim.max_tokens_per_call),
                                    "timeout_s": lim.timeout_s, "contexto": "~2–5 mil tokens (evidências + leituras)",
                                    "chamadas_por_tarefa": lim.max_calls_per_task, "prazo_tarefa_s": lim.task_deadline_s,
                                    "reenvios_conexao": lim.connect_retries, "reenvia_timeout": False}}
            if diag["modelo_configurado"]:
                diag["resultado"] = cli.probe()
            else:
                try:
                    diag["resultado"] = {"ok": True, "etapa": "listagem", "modelos": cli.list_models()}
                except Exception as e:  # noqa: BLE001
                    diag["resultado"] = {"ok": False, "etapa": "listagem", "tipo": type(e).__name__, "erro": str(e)[:400]}
        if getattr(args, "tarefa", False) and st.llm_enabled:
            diag["tarefa_composta"] = _llm_task_check(st, args.titulo)
        print(json.dumps(diag, ensure_ascii=False, indent=2, default=str))
        ok = isinstance(diag.get("resultado"), dict) and diag["resultado"].get("ok")
        if "tarefa_composta" in diag:
            ok = ok and diag["tarefa_composta"].get("ia", {}).get("estado") == "usada"
        return 0 if ok else 1
    rt = bootstrap(reingest=args.cmd == "ingest")
    if args.cmd in ("ingest", "status"):
        ok, _ = verify_chain(rt.conn)
        out = {"sim_now": rt.clock.now.isoformat(), "tick": rt.clock.tick, "audit_chain_ok": ok,
               "lotes_abertos": rt.conn.execute("SELECT count(*) FROM op_lotes WHERE status='aberto'").fetchone()[0],
               "casos": dict(rt.conn.execute("SELECT state, count(*) FROM cases GROUP BY state").fetchall()),
               "eventos_pendentes": rt.conn.execute("SELECT count(*) FROM events WHERE processed=0").fetchone()[0],
               "llm_habilitado": rt.settings.llm_enabled}
    elif args.cmd == "dq":
        out = [dict(r) | {"sample": json.loads(r["sample"])[:2]} for r in rt.conn.execute("SELECT * FROM data_quality ORDER BY check_id")]
    elif args.cmd == "run":
        cycles = rt.run(args.ticks)
        out = {"sim_now": rt.clock.now.isoformat(), "ciclos": len(cycles),
               "com_atividade": [x for x in cycles if x["lotes_fechados"] or x.get("acoes")]}
    elif args.cmd == "cases":
        out = [dict(r) for r in rt.conn.execute("SELECT case_id, rule_id, state, severity, certainty, exposure_cents/100.0 AS exposicao_rs, "
                                                "priority FROM cases ORDER BY priority DESC")]
    elif args.cmd == "case":
        row = rt.conn.execute("SELECT * FROM cases WHERE case_id=?", (args.case_id,)).fetchone()
        out = {"caso": dict(row) | {"body": json.loads(row["body"])},
               "transicoes": [dict(r) for r in rt.conn.execute("SELECT * FROM case_transitions WHERE case_id=? ORDER BY seq", (args.case_id,))],
               "decisoes": [dict(r) for r in rt.conn.execute("SELECT * FROM case_decisions WHERE case_id=? ORDER BY seq", (args.case_id,))]} if row else {}
    elif args.cmd == "placar":
        from .sim.scoreboard import compute
        out = _cents(compute(rt))
    elif args.cmd == "demo":
        if not rt.settings.demo_mode:
            print(json.dumps({"erro": "demo exige GP_DEMO_MODE=1"}, ensure_ascii=False))
            return 2
        from .sim.approver import ApprovalSimulator
        rt.approval_simulator = ApprovalSimulator(rt)
        rt.run(args.ticks)
        q = lambda sql: [dict(r) for r in rt.conn.execute(sql, (args.case,))]  # noqa: E731
        out = {"aviso": "aprovações produzidas pelo SIMULADOR DE TESTE (não são manifestações humanas reais)",
               "sim_now": rt.clock.now.isoformat(),
               "transicoes": q("SELECT sim_ts, from_state, to_state, actor, reason FROM case_transitions WHERE case_id=? ORDER BY seq"),
               "decisoes": q("SELECT sim_ts, decision, rationale, llm FROM case_decisions WHERE case_id=? ORDER BY seq"),
               "auditoria": [dict(r) for r in rt.conn.execute(
                   "SELECT seq, sim_ts, actor, actor_role, action, target, decision FROM audit_log WHERE idempotency_key LIKE ? "
                   "OR payload LIKE ? OR target IN (SELECT solicitacao_id FROM approval_requests WHERE params LIKE ?) ORDER BY seq",
                   (f"{args.case}:%", f"%{args.case.split('-', 1)[-1]}%", f"%{args.case.split('-', 1)[-1]}%"))],
               "decisoes_simuladas": [dict(r) for r in rt.conn.execute("SELECT * FROM simulated_decisions WHERE solicitacao_id IN "
                                      "(SELECT solicitacao_id FROM approval_requests WHERE params LIKE ?)", (f"%{args.case.split('-', 1)[-1]}%",))]}
    elif args.cmd == "aprovacoes":
        out = [dict(r) for r in rt.conn.execute("SELECT solicitacao_id, tool, requested_by, required, approvals, status FROM approval_requests "
                                                "WHERE status='pending' ORDER BY created_sim_ts")]
    else:
        # login simulado: no produto, a sessão é emitida pelo SSO após autenticar a pessoa
        if not rt.settings.demo_mode:
            print(json.dumps({"erro": "login por identificador só em modo demonstração (GP_DEMO_MODE=1); "
                                      "em produção a sessão vem do provedor de identidade"}, ensure_ascii=False))
            return 2
        token = rt.sessions.issue_demo(args.user, purpose="cli")
        if args.cmd == "aprovar":
            res = execute_with_session(rt, token, "aprovar_acao", {"solicitacao_id": args.solicitacao_id, "comentario": args.comentario})
        elif args.cmd == "recusar":
            res = execute_with_session(rt, token, "recusar_acao", {"solicitacao_id": args.solicitacao_id, "motivo": args.motivo})
        else:
            res = execute_with_session(rt, token, args.name, json.loads(args.params), idempotency_key=args.key, approval_id=args.approval)
        rt.sessions.revoke(token)
        out = res.model_dump()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
