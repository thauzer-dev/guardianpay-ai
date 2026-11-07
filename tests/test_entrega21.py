"""Entrega 2.1: continuidade após aprovação, IA no ciclo com guarda, limites/recuperação do LLM, integridade
pós-otimização, retomada idempotente e ausência de dupla contagem financeira."""
import json
from types import SimpleNamespace

import pytest

from guardianpay.agent.decision import Plan
from guardianpay.agent.reasoner import ContextualReasoner, Limits
from guardianpay.auth import AuthError, SessionManager
from guardianpay.auditing.context import AuditContext
from guardianpay.auditing.rules import run_rules
from guardianpay.config import load_settings
from guardianpay.domain.models import Finding
from guardianpay.llm.proxy_client import HttpResponse, LLMBudgetExceeded, LLMUnavailable, ProxyClient
from guardianpay.runtime import bootstrap
from guardianpay.sim.approver import TAG, ApprovalSimulator
from guardianpay.sim.scoreboard import attribution, compute

# Referência medida ANTES da otimização (perfil da Entrega 2, mesmo dado, LLM desligado)
BASELINE = {"pagamentos": 449, "lotes": 8, "casos": {"awaiting_approval": 46, "escalated": 2, "resolved": 10},
            "audit_exec": 149, "economia_real": 35_933_695, "economia_proj": 28_887_380, "atraso": 3_184_583}


def summary(rt):
    c = rt.conn
    return {"pagamentos": c.execute("SELECT count(*) FROM sim_payments").fetchone()[0],
            "lotes": c.execute("SELECT count(*) FROM lote_closures").fetchone()[0],
            "casos": dict(c.execute("SELECT state, count(*) FROM cases GROUP BY state").fetchall()),
            "audit_exec": c.execute("SELECT count(*) FROM audit_log WHERE decision='executed'").fetchone()[0],
            "economia_real": compute(rt)["economia_estimada_realizada_cents"],
            "economia_proj": compute(rt)["economia_estimada_projetada_cents"],
            "atraso": compute(rt)["custo_atraso_estimado_cents"]}


# ------------------------------------------------------------------ integridade após otimização
def test_full_simulation_matches_pre_optimization_baseline(rt):
    rt.run()
    assert rt.clock.finished
    assert summary(rt) == BASELINE
    assert rt.conn.execute("SELECT count(*) FROM op_lotes WHERE status='aberto'").fetchone()[0] == 9  # fecham após 09/11 08h
    assert rt.conn.execute("SELECT count(*) FROM events WHERE processed<>1").fetchone()[0] == 0


def test_restart_mid_simulation_is_idempotent(rt):
    rt.run(30)
    rt2 = bootstrap(load_settings(db_path=rt.settings.db_path, raw_dir=rt.settings.raw_dir, ai_proxy_api_key=None, demo_mode=True))
    assert rt2.clock.tick == 30
    rt2.run(0)                                     # ciclo do tique atual já feito: não repete
    rt2.run()
    assert summary(rt2) == BASELINE
    assert rt2.conn.execute("SELECT count(*) FROM (SELECT idempotency_key FROM audit_log WHERE decision='executed' "
                            "AND idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING count(*)>1)").fetchone()[0] == 0


# ------------------------------------------------------------------ continuidade após aprovação (simulador de teste)
def test_end_to_end_approval_flow_with_explicit_test_simulator(rt):
    rt.approval_simulator = ApprovalSimulator(rt)
    rt.run(30)                                     # 02/11 08h -> 03/11 14h
    cid = "R2-TP431459"
    trans = [r["to_state"] for r in rt.conn.execute("SELECT to_state FROM case_transitions WHERE case_id=? ORDER BY seq", (cid,))]
    assert trans == ["detected", "investigating", "authorized", "executing", "awaiting_approval",
                     "authorized", "executing", "resolved"]
    decisions = [r["decision"] for r in rt.conn.execute("SELECT decision FROM case_decisions WHERE case_id=? ORDER BY seq", (cid,))]
    assert decisions == ["conter", "revalidacao_ok"]
    sol = rt.conn.execute("SELECT * FROM approval_requests WHERE tool='corrigir_lancamento' AND params LIKE '%TP431459%'").fetchone()
    routed = json.loads(sol["routed_to"])
    assert routed[0]["requisito"] == {"role": "aprovador"} and routed[0]["candidatos"][0] == "usuario_12"   # pessoa correta
    appr = json.loads(sol["approvals"])
    assert appr[0]["actor_id"] == "usuario_12" and appr[0]["comentario"].startswith(TAG)                   # marcado como teste
    sim = rt.conn.execute("SELECT * FROM simulated_decisions WHERE solicitacao_id=?", (sol["solicitacao_id"],)).fetchone()
    assert sim["decision"] == "aprovar" and sim["sim_ts"] >= "2026-11-03T08:00:00"                          # só em expediente
    assert rt.conn.execute("SELECT count(*) FROM audit_log WHERE action='corrigir_lancamento' AND decision='executed' "
                           "AND payload LIKE ?", (f'%{sol["solicitacao_id"]}%',)).fetchone()[0] == 1
    t = rt.conn.execute("SELECT * FROM op_titulos WHERE titulo_id='TP431459'").fetchone()
    assert (t["valor_cents"], t["status"], t["lote_remessa"]) == (483_800, "agendado", "RM20261105-MER")


def test_rejection_is_respected_and_not_executed(rt):
    rt.approval_simulator = ApprovalSimulator(rt)
    rt.run(30)
    c = rt.conn.execute("SELECT state FROM cases WHERE case_id='R6-TP433196'").fetchone()
    assert c["state"] == "escalated"
    assert rt.conn.execute("SELECT status FROM op_titulos WHERE titulo_id='TP433196'").fetchone()[0] == "aberto"
    assert rt.conn.execute("SELECT count(*) FROM audit_log WHERE action='agendar_titulo' AND decision='executed' "
                           "AND target='TP433196'").fetchone()[0] == 0


def test_revalidation_blocks_execution_if_state_changed(rt):
    rt.cycle()
    rt.conn.execute("UPDATE op_titulos SET version=version+1 WHERE titulo_id='TP431459'")   # alteração por terceiro
    sid = json.loads(rt.conn.execute("SELECT pending_request FROM cases WHERE case_id='R2-TP431459'").fetchone()[0])["approvals"][0]["solicitacao_id"]
    from guardianpay.auth import execute_with_session
    tok = rt.sessions.issue_demo("usuario_12", purpose="teste")
    assert execute_with_session(rt, tok, "aprovar_acao", {"solicitacao_id": sid}).data["status"] == "approved"
    rt.advance(1); rt.cycle()
    assert rt.conn.execute("SELECT state FROM cases WHERE case_id='R2-TP431459'").fetchone()[0] == "escalated"
    assert rt.conn.execute("SELECT valor_cents FROM op_titulos WHERE titulo_id='TP431459'").fetchone()[0] == 4_838_000


def test_simulator_and_demo_login_require_demo_mode(rt):
    rt_prod = SimpleNamespace(settings=SimpleNamespace(demo_mode=False))
    with pytest.raises(PermissionError):
        ApprovalSimulator(rt_prod)
    prod = SessionManager(rt.conn, rt.clock, rt.actors, demo_mode=False)
    with pytest.raises(AuthError):
        prod.issue_demo("usuario_12", purpose="x")
    tok = rt.sessions.issue_demo("usuario_12", purpose="x")
    with pytest.raises(AuthError):
        prod.resolve(tok)                          # sessão demo não vale fora do modo demonstração


def test_identity_provider_path_maps_verified_email(rt):
    prod = SessionManager(rt.conn, rt.clock, rt.actors, demo_mode=False)
    email = rt.conn.execute("SELECT email FROM raw_usuarios_sistema WHERE login='usuario_12'").fetchone()[0]
    tok = prod.issue_from_identity_provider("jwt", lambda _t: {"email": email, "email_verified": True, "iss": "https://sso"})
    assert prod.resolve(tok) == "usuario_12"
    with pytest.raises(AuthError):
        prod.issue_from_identity_provider("jwt", lambda _t: {"email": email, "email_verified": False})
    with pytest.raises(AuthError):
        prod.issue_from_identity_provider("jwt", lambda _t: (_ for _ in ()).throw(ValueError("assinatura")))


# ------------------------------------------------------------------ AI Proxy: falha e recuperação, limites
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Transport:
    def __init__(self, script):
        self.script = list(script)

    def __call__(self, method, url, headers, body, timeout):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


MODELS = HttpResponse(200, {}, json.dumps({"data": [{"id": "glm-5.3-flash"}]}).encode())


def chat_resp(obj):
    return HttpResponse(200, {"x-model-served": "glm-5.3-flash"}, json.dumps(
        {"choices": [{"message": {"content": json.dumps(obj)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode())


def client(rt, script, **kw):
    s = load_settings(db_path=rt.settings.db_path, raw_dir=rt.settings.raw_dir, ai_proxy_api_key="gw-tok-teste", ai_proxy_model="")
    return ProxyClient(s, rt.conn, transport=Transport(script), sleeper=lambda _: None, **kw)


def test_proxy_failure_then_recovery_after_cooldown(rt):
    clk = Clock()
    cli = client(rt, [OSError("x")] * 2 + [MODELS, chat_resp({"type": "plan"})], max_retries=0, breaker_cooldown_s=60, now_fn=clk)
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            cli.list_models()
    assert not cli.available
    with pytest.raises(LLMUnavailable):
        cli.chat([{"role": "user", "content": "x"}])       # aberto: nem tenta a rede
    clk.t = 61
    assert cli.available                                     # meio-aberto: uma chamada de teste
    assert cli.chat([{"role": "user", "content": "x"}]).model == "glm-5.3-flash"
    assert cli.available and not cli._breaker_open           # recuperado


def test_llm_global_budget_enforced(rt):
    cli = client(rt, [MODELS] + [chat_resp({"type": "plan"})] * 5, max_calls=2)
    cli.chat([{"role": "user", "content": "x"}]); cli.chat([{"role": "user", "content": "x"}])
    with pytest.raises(LLMBudgetExceeded):
        cli.chat([{"role": "user", "content": "x"}])
    assert not cli.available


class FakeLLM:
    """LLM roteirizado (mock) com a mesma interface do ProxyClient."""
    def __init__(self, replies):
        self.replies, self.calls, self.available, self.budget_exhausted = list(replies), 0, True, False

    def chat(self, messages, **kw):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else {"type": "investigate", "requests": []}
        return SimpleNamespace(content=json.dumps(r), model="mock")


def _case(rt, cid):
    f = {x.case_id: x for x in run_rules(AuditContext(rt.conn, rt.clock.now.isoformat()))}[cid]
    from guardianpay.agent.decision import plan_for
    ctx = AuditContext(rt.conn, rt.clock.now.isoformat())
    t = {r["titulo_id"]: dict(r) for r in rt.conn.execute("SELECT * FROM op_titulos")}
    return f, plan_for(f, t, ctx.lote_open, lambda _r: None)


def test_reasoner_step_and_call_limits(rt):
    llm = FakeLLM([{"type": "investigate", "requests": [{"tool": "consultar_titulo", "params": {"titulo_id": "TP432863"}}]}] * 10)
    r = ContextualReasoner(rt, llm, Limits(max_steps=5, max_calls_per_case=2))
    f, plan = _case(rt, "R3-TP432863")
    out = r.refine(f, plan, f.case_id)
    assert llm.calls == 2 and out.note.startswith("indisponivel:limite de chamadas")
    assert [a.tool for a in out.plan.actions] == [a.tool for a in plan.actions]       # fallback determinístico intacto


def test_ai_plan_is_validated_and_policy_rejections_recorded(rt):
    f, plan = _case(rt, "R3-TP432863")
    llm = FakeLLM([
        {"type": "investigate", "requests": [
            {"tool": "consultar_extrato", "params": {"banco": "Banco Litoral", "valor": "27134.80"}},
            {"tool": "cancelar_titulo", "params": {"titulo_id": "TP432863"}}]},          # mutação como "leitura": recusada
        {"type": "plan", "assessment": "Pagamento manual em 28/10 confirmado pelo extrato (T1) e DDA (E6).",
         "missing_information": ["comprovante de baixa no ERP"], "ambiguity": False, "references": ["T1", "E6"],
         "proposed_actions": [
             {"tool": "liberar_lote", "params": {"lote_remessa": "RM20261106-MER"}, "evidence_refs": ["E1"]},
             {"tool": "bloquear_titulo", "params": {"titulo_id": "TP426904", "motivo": "x"}, "evidence_refs": ["E1"]},
             {"tool": "acionar_pessoa", "params": {"pessoa_id": "PS09", "motivo": "x"}, "evidence_refs": ["E99"]},
             {"tool": "retirar_do_lote", "params": {"titulo_id": "TP432863", "motivo": "x", "actor_id": "usuario_15"}, "evidence_refs": ["E1"]},
             {"tool": "contatar_fornecedor", "params": {"fornecedor_id": f.fornecedor_id, "canal": "email",
                                                       "mensagem": "Confirmam o recebimento de 28/10?"}, "evidence_refs": ["T1"]}]}])
    out = ContextualReasoner(rt, llm, Limits(max_ai_actions=5)).refine(f, plan, f.case_id)
    reasons = {r["tool"]: r["motivo"] for r in out.rejected}
    assert "aprova" in reasons["liberar_lote"]
    assert "escopo" in reasons["bloquear_titulo"]
    assert "evidência" in reasons["acionar_pessoa"]
    assert "identidade" in reasons["retirar_do_lote"]
    assert [a["tool"] for a in out.accepted] == ["contatar_fornecedor"]
    assert [a.tool for a in out.plan.actions][: len(plan.actions)] == [a.tool for a in plan.actions]   # piso mantido
    assert out.plan.actions[-1].actor_id == "agent:orchestrator"                                        # identidade do sistema
    trace = [r["kind"] for r in rt.conn.execute("SELECT kind FROM ai_traces WHERE case_id=? ORDER BY seq", (f.case_id,))]
    assert trace == ["resposta", "leitura", "leitura", "resposta", "validacao"]
    assert rt.conn.execute("SELECT validation FROM ai_traces WHERE kind='leitura' AND content LIKE '%cancelar%'").fetchone()[0].startswith("rejeitado")


def test_ai_containment_on_weak_evidence_is_blocked_by_risk_gate(rt):
    f, plan = _case(rt, "R3-TP432863")
    weak = Finding.model_validate({**f.model_dump(), "certainty": "indicio", "strength": "moderada"})
    llm = FakeLLM([{"type": "plan", "assessment": "a", "proposed_actions": [
        {"tool": "bloquear_titulo", "params": {"titulo_id": "TP432863", "motivo": "x"}, "evidence_refs": ["E1"]}]}])
    out = ContextualReasoner(rt, llm).refine(weak, Plan("monitorar", "r", [], "investigating"), weak.case_id)
    assert out.accepted == [] and "portão de risco" in out.rejected[0]["motivo"]


def test_orchestrator_executes_accepted_ai_action_through_executor(rt):
    llm = FakeLLM([{"type": "plan", "assessment": "ok", "ambiguity": True, "escalate_to": ["PS09"], "references": ["E1"],
                    "proposed_actions": []}] * 20)
    rt.orchestrator.llm = llm
    rt.cycle()
    note = rt.conn.execute("SELECT llm FROM case_decisions WHERE case_id='R3-TP432863'").fetchone()[0]
    assert note.startswith("ia:") and "ambiguidade=True" in note
    assert rt.conn.execute("SELECT count(*) FROM audit_log WHERE action='acionar_pessoa' AND idempotency_key='R3-TP432863:ia:acionar_pessoa:PS09' "
                           "AND decision='executed'").fetchone()[0] == 1
    used = rt.conn.execute("SELECT count(*) FROM case_decisions WHERE llm LIKE 'ia:%'").fetchone()[0]
    assert llm.calls == used and used <= 8         # só casos elegíveis (R3/R4/R5/R5F/indícios) chamam o LLM


# ------------------------------------------------------------------ sem dupla contagem
def test_no_double_counting_across_findings(rt):
    rt.run(30)
    before = compute(rt)
    body = json.loads(rt.conn.execute("SELECT body FROM cases WHERE case_id='R5-TP431754'").fetchone()[0])
    body.update(case_id="FAKE-DUP", rule_id="R6_APROVACAO_DUPLA_AUSENTE")       # 2º achado sobre o MESMO título (teste)
    rt.conn.execute("INSERT INTO cases(case_id,rule_id,state,titulos,severity,exposure_cents,certainty,body,created_sim_ts,updated_sim_ts) "
                    "VALUES('FAKE-DUP','R6_APROVACAO_DUPLA_AUSENTE','escalated',?, 'alta', ?, 'comprovado', ?, 'x','x')",
                    (json.dumps(body["titulos"]), body["exposure_cents"], json.dumps(body)))
    after = compute(rt)
    assert after["exposicao_potencial_cents"] == before["exposicao_potencial_cents"]
    assert after["economia_estimada_realizada_cents"] == before["economia_estimada_realizada_cents"]
    attr = attribution(rt.conn)
    assert attr["TP431754"][0].case_id == "R5-TP431754"   # regra de maior prioridade fica com o título
    assert len(attr) == len(set(attr))
