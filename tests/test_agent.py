"""Testes obrigatórios da Entrega 2 (agente, auditoria, tempo, aprovação, LLM, retomada, autenticação).

Os testes rodam sobre CÓPIAS do banco (fixture rt). Onde um cenário não existe na base fictícia, o teste altera
somente a cópia operacional/derivada e diz isso explicitamente (dado sintético de teste).
"""
import json

import pytest

from guardianpay import events
from guardianpay.agent.decision import plan_for
from guardianpay.agent.orchestrator import _SimulatedCrash
from guardianpay.auditing.context import AuditContext
from guardianpay.auditing.rules import run_rules
from guardianpay.auth import execute_with_session
from guardianpay.config import load_settings
from guardianpay.domain.models import Certainty, Evidence, Finding, Severity, Strength
from guardianpay.llm.proxy_client import HttpResponse, LLMMalformed, LLMUnavailable, ProxyClient
from guardianpay.runtime import bootstrap
from guardianpay.sim.scoreboard import compute
from guardianpay.tools.contract import ToolRequest, ToolStatus


def t_state(rt, tid):
    return dict(rt.conn.execute("SELECT * FROM op_titulos WHERE titulo_id=?", (tid,)).fetchone())


def case(rt, cid):
    r = rt.conn.execute("SELECT * FROM cases WHERE case_id=?", (cid,)).fetchone()
    return dict(r) if r else None


def executed(rt, action, target=None):
    sql, a = "SELECT count(*) FROM audit_log WHERE action=? AND decision='executed'", [action]
    if target:
        sql += " AND target=?"; a.append(target)
    return rt.conn.execute(sql, a).fetchone()[0]


def findings(rt):
    return {f.case_id: f for f in run_rules(AuditContext(rt.conn, rt.clock.now.isoformat()))}


# ------------------------------------------------------------------ 1. duplicidade confirmada
def test_confirmed_duplicate_is_contained_and_cancel_requested(rt):
    f = findings(rt)["R1-TP430006"]          # NF 20329 parcela 2/2 digitada '20329' e '000020329'
    assert f.certainty is Certainty.comprovado and f.titulos == ["TP430006", "TP429834"]
    rt.cycle()
    assert t_state(rt, "TP430006")["status"] == "aberto"                  # retirado do lote
    assert t_state(rt, "TP429834")["status"] == "agendado"                # original segue
    c = case(rt, "R1-TP430006")
    assert c["state"] == "awaiting_approval"                             # contido, NÃO resolvido
    pend = json.loads(c["pending_request"])["approvals"]
    assert pend and pend[0]["action"]["tool"] == "cancelar_titulo"
    assert executed(rt, "retirar_do_lote", "TP430006") == 1


# ------------------------------------------------------------------ 2. suspeita falsa ou ambígua
def test_weak_or_ambiguous_signal_does_not_block():
    f = Finding(case_id="X", rule_id="R3_PAGO_ANTERIORMENTE", titulos=["TP1"], evidences=[Evidence(source="s", ref="r")],
                severity=Severity.media, exposure_cents=100_000, certainty=Certainty.indicio, strength=Strength.moderada,
                recommended_action="retirar_do_lote")
    plan = plan_for(f, {"TP1": {"status": "agendado", "lote_remessa": "L", "bloqueado": 0, "valor_cents": 100_000}},
                    lambda _: True, lambda _: None)
    assert plan.decision == "monitorar" and plan.actions == []


def test_same_number_different_series_is_not_duplicate(rt):
    # sintético (só na cópia de teste): segunda NF com o mesmo número e outra série para o fornecedor de TP430006
    nf = dict(rt.conn.execute("SELECT * FROM n_nf WHERE cnpj_root='52759405' AND nf_num='20329'").fetchone())
    nf.update(chave_acesso=nf["chave_acesso"][:-1] + "X", serie="2")
    rt.conn.execute(f"INSERT INTO n_nf ({','.join(nf)}) VALUES ({','.join('?' * len(nf))})", list(nf.values()))
    rt.conn.execute("UPDATE n_titulos SET nf_sufixo='2' WHERE titulo_id='TP430006'")
    rt.conn.execute("UPDATE n_titulos SET nf_sufixo='1' WHERE titulo_id='TP429834'")
    assert "R1-TP430006" not in findings(rt)


# ------------------------------------------------------------------ 3. divergência legítima
def test_legitimate_differences_are_not_flagged(rt):
    f = findings(rt)
    assert "R2-TP428333" not in f              # 1 centavo de arredondamento entre parcelas
    assert "R5-TP430006" not in f and not any(k.startswith("R5-") and "Nordplast" in v.summary for k, v in f.items())
    # multa 2% + juros pro rata: título pago 3 dias após a duplicata (lote 03/11, duplicata 31/10)
    dup = rt.conn.execute("SELECT d.valor_cents FROM n_titulos t JOIN n_nf n ON n.cnpj_root=t.fornecedor_cnpj_root AND n.nf_num=t.nf_num "
                          "JOIN n_duplicatas d ON d.chave_acesso=n.chave_acesso AND d.n_dup=t.parcela_n WHERE t.titulo_id='TP426904'").fetchone()[0]
    rt.conn.execute("UPDATE op_titulos SET valor_cents=? WHERE titulo_id='TP426904'", (int(dup * 1.02),))
    assert "R2-TP426904" not in findings(rt)
    rt.conn.execute("UPDATE op_titulos SET valor_cents=? WHERE titulo_id='TP426904'", (int(dup * 1.05),))
    assert findings(rt)["R2-TP426904"].rule_id == "R2_VALOR_ACIMA_DUPLICATA"


def test_confirmed_bank_change_with_previous_phone_is_not_flagged(rt):
    f = findings(rt)
    assert not any(k.startswith("R4-FP3345") for k in f)    # confirmado no telefone anterior à alteração
    assert not any(k.startswith("R4-FP5079") for k in f)


# ------------------------------------------------------------------ 4. alteração bancária não confirmada
def test_unconfirmed_bank_change_blocks_and_requests_dual_reversal(rt):
    f = findings(rt)["R4-FP4615-AC31202"]
    assert "dominio_semelhante_ao_cadastrado" in f.signals and set(f.notify) == {"PS01", "PS08"}
    rt.cycle()
    blocked = rt.conn.execute("SELECT count(*) FROM op_titulos WHERE fornecedor_id='FP4615' AND bloqueado=1").fetchone()[0]
    assert blocked == len(f.titulos)
    sol = rt.conn.execute("SELECT * FROM approval_requests WHERE tool='reverter_alteracao_cadastro'").fetchone()
    assert sol["status"] == "pending" and json.loads(sol["required"]) == [{"role": "aprovador"}, {"pessoa_id": "PS01"}]
    assert rt.conn.execute("SELECT banco FROM op_fornecedores WHERE fornecedor_id='FP4615'").fetchone()[0] == "905"  # não reverteu sozinho
    assert {r[0] for r in rt.conn.execute("SELECT to_ref FROM outbox WHERE kind='acionamento' AND body LIKE '%FP4615%'")} == {"PS01", "PS08"}
    assert case(rt, "R4-FP4615-AC31202")["state"] == "awaiting_approval"


# ------------------------------------------------------------------ 5/6. retenção antes e depois do corte
def test_containment_before_cutoff_keeps_money_in(rt):
    rt.cycle()                                   # 02/11 08:00 — feriado; lote de 03/11 fecha às 10:00
    assert t_state(rt, "TP431754")["bloqueado"] == 1
    rt.run(26)                                   # até 03/11 10:00
    assert rt.conn.execute("SELECT status FROM op_lotes WHERE lote_remessa='RM20261103-MER'").fetchone()[0] == "enviado"
    assert rt.conn.execute("SELECT count(*) FROM sim_payments WHERE titulo_id='TP431754'").fetchone()[0] == 0
    assert rt.conn.execute("SELECT count(*) FROM sim_payments WHERE titulo_id='TP429834'").fetchone()[0] == 1   # legítimo saiu
    sb = compute(rt)
    assert sb["perda_simulada_cents"] == 0 and sb["evitado_realizado_cents"] >= 1_174_000


def test_containment_after_cutoff_is_denied(rt):
    rt.advance(26)                               # 03/11 10:00 sem o agente agir
    from guardianpay.sim.scheduler import close_due_lots
    close_due_lots(rt)
    r = rt.executor.execute(ToolRequest(tool="retirar_do_lote", actor_id="agent:orchestrator", params={"titulo_id": "TP429834", "motivo": "x"}))
    assert r.status is ToolStatus.denied           # já pago no lote enviado
    # lote ainda 'aberto' no cadastro mas com horário de corte vencido: também negado (relógio manda)
    r = rt.executor.execute(ToolRequest(tool="bloquear_titulo", actor_id="agent:risk_fraud",
                                        params={"titulo_id": "TP426904", "motivo": "x"}))
    assert r.status is ToolStatus.denied
    rt.conn.execute("UPDATE op_lotes SET status='aberto' WHERE lote_remessa='RM20261103-MER'")
    rt.conn.execute("UPDATE op_titulos SET status='agendado', lote_remessa='RM20261103-MER' WHERE titulo_id='TP426988'")
    r = rt.executor.execute(ToolRequest(tool="retirar_do_lote", actor_id="agent:orchestrator", params={"titulo_id": "TP426988", "motivo": "x"}))
    assert r.status is ToolStatus.denied and "corte" in r.reasons[0]


# ------------------------------------------------------------------ 7. aprovação humana obrigatória
def test_human_approval_required_then_executed_and_verified(rt):
    rt.cycle()
    c = case(rt, "R2-TP431459")                   # título R$ 48.380,00 x duplicata R$ 4.838,00
    assert c["state"] == "awaiting_approval" and t_state(rt, "TP431459")["status"] == "aberto"
    sid = json.loads(c["pending_request"])["approvals"][0]["solicitacao_id"]
    # agente não aprova; analista também não
    assert rt.executor.execute(ToolRequest(tool="aprovar_acao", actor_id="agent:orchestrator", params={"solicitacao_id": sid})).status is ToolStatus.denied
    tok_an = rt.sessions.issue("usuario_01", issued_by="teste")
    assert execute_with_session(rt, tok_an, "aprovar_acao", {"solicitacao_id": sid}).status is ToolStatus.denied
    tok = rt.sessions.issue("usuario_12", issued_by="teste")
    assert execute_with_session(rt, tok, "aprovar_acao", {"solicitacao_id": sid}).data["status"] == "approved"
    rt.advance(1); rt.cycle()
    t = t_state(rt, "TP431459")
    assert t["valor_cents"] == 483_800 and t["status"] == "agendado" and t["lote_remessa"] == "RM20261105-MER"
    assert case(rt, "R2-TP431459")["state"] == "resolved"


def test_small_difference_corrected_without_human(rt):
    rt.cycle()
    assert t_state(rt, "TP431634")["valor_cents"] == 240_636      # R$ 240,64 -> duplicata R$ 2.406,36 (≤ alçada)
    assert case(rt, "R2-TP431634")["state"] == "resolved"
    assert t_state(rt, "TP431634")["status"] == "agendado"         # sem atraso


# ------------------------------------------------------------------ 8. falha / indisponibilidade do LLM
class FakeTransport:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


MODELS = HttpResponse(200, {}, json.dumps({"data": [{"id": "deepseek-v4-pro"}, {"id": "glm-5.3-flash"}, {"id": "latest"}]}).encode())
CHAT_OK = HttpResponse(200, {"x-model-served": "glm-5.3-flash"}, json.dumps(
    {"choices": [{"message": {"content": "Risco alto."}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())


def _client(rt, script, **kw):
    s = load_settings(db_path=rt.settings.db_path, raw_dir=rt.settings.raw_dir, ai_proxy_api_key="gw-tok-teste", ai_proxy_model="")
    sleeps = []
    return ProxyClient(s, rt.conn, transport=FakeTransport(script), sleeper=sleeps.append, **kw), sleeps


def test_llm_model_discovery_retry_and_ratelimit(rt):
    cli, sleeps = _client(rt, [MODELS, HttpResponse(503, {}, b""), HttpResponse(429, {"retry-after": "3"}, b""), CHAT_OK],
                          preference=("deepseek-v4-pro", "glm-5.3-flash"))
    r = cli.chat([{"role": "user", "content": "x"}])
    assert r.model == "glm-5.3-flash" and "deepseek" not in "".join(cli.list_models())   # nunca escolhe deepseek
    assert sleeps == [2, 3.0]                       # backoff no 503 e retry-after no 429
    assert tuple(rt.conn.execute("SELECT input_tokens, output_tokens FROM llm_usage WHERE status='ok'").fetchone()) == (10, 5)


def test_llm_does_not_assume_latest(rt):
    s = load_settings(db_path=rt.settings.db_path, raw_dir=rt.settings.raw_dir, ai_proxy_api_key="k", ai_proxy_model="")
    cli = ProxyClient(s, rt.conn, transport=FakeTransport([HttpResponse(200, {}, b'{"data":[{"id":"latest"}]}')]),
                      sleeper=lambda _: None, preference=("glm-5.3-flash",))
    with pytest.raises(LLMUnavailable):
        cli.resolve_model()


def test_llm_malformed_and_breaker(rt):
    cli, _ = _client(rt, [MODELS, HttpResponse(200, {}, b"<html>"), OSError("rede"), OSError("rede"), OSError("rede")], max_retries=2)
    with pytest.raises(LLMMalformed):
        cli.chat([{"role": "user", "content": "x"}])
    with pytest.raises(LLMUnavailable):
        cli.chat([{"role": "user", "content": "x"}])
    assert not cli.available                       # disjuntor aberto: modo determinístico
    assert "gw-tok-teste" not in repr(cli)


def test_agent_works_when_llm_unavailable(rt):
    cli, _ = _client(rt, [OSError("rede")] * 10, max_retries=1)
    rt.orchestrator.llm = cli
    rt.cycle()
    assert t_state(rt, "TP431754")["bloqueado"] == 1
    llm_notes = [r[0] for r in rt.conn.execute("SELECT llm FROM case_decisions WHERE case_id IN ('R5-TP431754','R4-FP4615-AC31202')")]
    assert llm_notes and all(n.startswith("indisponivel") for n in llm_notes)


# ------------------------------------------------------------------ 9. reinício durante a investigação
def test_restart_mid_execution_resumes_without_repeating(rt):
    rt.orchestrator.crash_after_actions = 3
    with pytest.raises(_SimulatedCrash):
        rt.cycle()
    crashed = [r["case_id"] for r in rt.conn.execute("SELECT case_id FROM cases WHERE state='executing'")]
    assert len(crashed) == 1
    before = rt.conn.execute("SELECT count(*) FROM audit_log WHERE decision='executed'").fetchone()[0]
    rt2 = bootstrap(load_settings(db_path=rt.settings.db_path, raw_dir=rt.settings.raw_dir, ai_proxy_api_key=None))
    rt2.cycle()
    assert case(rt2, crashed[0])["state"] in ("awaiting_approval", "escalated", "resolved")
    dup = rt2.conn.execute("SELECT idempotency_key, count(*) n FROM audit_log WHERE decision='executed' "
                           "GROUP BY idempotency_key HAVING n>1").fetchall()
    assert dup == []                               # nenhuma operação repetida
    assert rt2.conn.execute("SELECT count(*) FROM audit_log WHERE decision='executed'").fetchone()[0] > before


# ------------------------------------------------------------------ 10. repetição de evento
def test_event_replay_does_not_duplicate_actions(rt):
    rt.cycle()
    n_exec = rt.conn.execute("SELECT count(*) FROM audit_log WHERE decision='executed'").fetchone()[0]
    eid, created = events.emit(rt.conn, type_=events.EventType.snapshot_carregado, source="ingest", sim_ts="2026-11-02T08:00:00",
                               payload={"files": 24}, trusted=True)
    assert not created                              # emissão idempotente
    rt.conn.execute("UPDATE events SET processed=0")   # redelivery forçado de TODOS os eventos
    rt.conn.execute("UPDATE cases SET state='investigating' WHERE state='awaiting_approval' AND rule_id='R1_DUPLICIDADE'")
    rt.cycle(); rt.cycle()
    assert rt.conn.execute("SELECT count(*) FROM audit_log WHERE decision='executed'").fetchone()[0] == n_exec
    assert rt.conn.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == \
        rt.conn.execute("SELECT count(DISTINCT solicitacao_id) FROM approval_requests").fetchone()[0]


# ------------------------------------------------------------------ 11. ator não autenticado / sem permissão
def test_unauthenticated_or_unauthorized_actor(rt):
    p = {"titulo_id": "TP426904", "motivo": "x"}
    assert execute_with_session(rt, None, "retirar_do_lote", p).status is ToolStatus.denied
    assert execute_with_session(rt, "token-forjado", "retirar_do_lote", p).status is ToolStatus.denied
    tok = rt.sessions.issue("usuario_01", issued_by="teste")      # analista
    r = execute_with_session(rt, tok, "bloquear_titulo", {**p, "actor_id": "usuario_15"})   # tenta se passar por diretora
    assert r.status is ToolStatus.denied and "analista_cp" in r.reasons[0]
    r = execute_with_session(rt, tok, "retirar_do_lote", {**p, "actor_id": "agent:risk_fraud"})
    assert r.status is ToolStatus.ok
    assert rt.conn.execute("SELECT actor FROM audit_log WHERE action='retirar_do_lote'").fetchone()[0] == "usuario_01"
    with pytest.raises(PermissionError):
        rt.sessions.issue("usuario_02", issued_by="teste")       # desligado
    with pytest.raises(PermissionError):
        rt.sessions.issue("agent:orchestrator", issued_by="teste")
    rt.sessions.revoke(tok)
    assert execute_with_session(rt, tok, "retirar_do_lote", p).status is ToolStatus.denied


def test_prompt_injection_in_supplier_email_is_never_authority(rt):
    rt.cycle()
    # e-mail do domínio semelhante diz que a troca 'já foi validada': o agente bloqueia mesmo assim
    assert t_state(rt, "TP432130")["bloqueado"] == 1
    assert rt.conn.execute("SELECT count(*) FROM approval_requests WHERE status='approved'").fetchone()[0] == 0
    assert case(rt, "R7-EM570996")["state"] == "resolved"   # instrução a automação registrada como incidente
