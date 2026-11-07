"""Correção crítica do runtime LLM: disjuntor CLOSED/OPEN/HALF_OPEN, orçamento por instância/canal/tarefa,
timeout sem reenvio, classificação de falhas, diagnóstico sem credencial, isolamento entre instâncias e a tarefa
composta do Agent Workspace usando o ProxyClient REAL sobre um transporte HTTP SIMULADO (MOCK identificado:
nenhuma chamada de rede acontece nestes testes)."""
import json
import sqlite3
from dataclasses import replace

import pytest

from conftest import RAW
from guardianpay.api.agent_chat import AgentChat
from guardianpay.config import load_settings
from guardianpay.llm.breaker import CLOSED, HALF_OPEN, OPEN, PERMANENT, TRANSIENT, CircuitBreaker
from guardianpay.llm.budget import AUTONOMOUS, INTERACTIVE, InstanceBudget, LLMLimits, TaskBudget
from guardianpay.llm.jsonout import extract_json
from guardianpay.llm.proxy_client import (HttpResponse, LLMBudgetExceeded, LLMCircuitOpen, LLMForbidden, LLMMalformed,
                                          LLMTimeout, LLMTruncated, LLMUnavailable, ProxyClient)
from guardianpay.runtime import bootstrap

ACCEPT = ("Investigue o título TP431754. Cruze os dados de contas a pagar, fornecedor e DDA, identifique inconsistências "
          "e explique quais evidências sustentam suas conclusões.")
EXEC = "Investigue o título TP431754, cruze os dados e execute as ações autorizadas"
SECRET = "gw-tok-SEGREDO-RUNTIME-999"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def ok(content, *, model="gpt-6-astra", inp=1200, out=300, finish="stop", content_is_list=False):
    msg = {"content": [{"type": "text", "text": content}] if content_is_list else content}
    return HttpResponse(200, {"x-model-served": model, "x-ratelimit-remaining": "240"}, json.dumps(
        {"model": model, "choices": [{"message": msg, "finish_reason": finish}],
         "usage": {"prompt_tokens": inp, "completion_tokens": out, "completion_tokens_details": {"reasoning_tokens": 40}}}).encode())


class Transport:
    """MOCK do gateway. Itens: HttpResponse, Exception ou callable(body)->HttpResponse. `latency` simula o tempo de
    resposta do modelo em função do tamanho do pedido: acima do timeout => TimeoutError (como o urllib)."""
    def __init__(self, script=(), latency=None, default=None):
        self.script, self.latency, self.default, self.calls = list(script), latency, default, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body) if body else None, "timeout": timeout})
        if self.latency is not None and self.latency(body or b"") > timeout:
            raise TimeoutError("The read operation timed out")
        item = self.script.pop(0) if self.script else self.default
        if callable(item) and not isinstance(item, HttpResponse):
            item = item(json.loads(body) if body else None)
        if isinstance(item, Exception):
            raise item
        return item


def settings(rt=None, **kw):
    base = load_settings(raw_dir=RAW, ai_proxy_api_key=SECRET, ai_proxy_model="gpt-6-astra", llm_mode="auto")
    if rt is not None:
        base = replace(base, db_path=rt.settings.db_path)
    return replace(base, **kw) if kw else base


def client(conn=None, script=(), clock=None, limits=None, **kw):
    t = kw.pop("transport", None) or Transport(script)
    cli = ProxyClient(settings(), conn, transport=t, sleeper=kw.pop("sleeper", lambda _: None), now_fn=clock or Clock(),
                      limits=limits or LLMLimits(), max_retries=kw.pop("max_retries", 1), **kw)
    return cli, t


def attach(rt, cli):
    rt.llm = cli
    rt.orchestrator.llm = cli
    return AgentChat(rt)


def assessment(refs=("E1",), actions=()):
    return json.dumps({"type": "assessment",
                       "assessment": "O boleto do ERP paga um CNPJ de raiz diferente da do fornecedor e o DDA registra outro boleto.",
                       "inconsistencies": [{"sistemas": ["ERP", "fornecedor", "DDA"], "descricao": "beneficiário divergente", "refs": list(refs)}],
                       "missing": [], "proposed_actions": list(actions)})


def no_secret(obj):
    txt = json.dumps(obj, ensure_ascii=False, default=str)
    return SECRET not in txt and "Authorization" not in txt and "Bearer" not in txt


def audit_n(rt):
    return rt.conn.execute("SELECT coalesce(max(seq),0) FROM audit_log").fetchone()[0]


# ====================================================================== 0. causa-raiz reproduzida
def test_root_cause_old_runtime_parameters_time_out_and_open_breaker(rt):
    """Probe (16 tok) cabe em 20 s; a investigação composta (contexto de milhares de tokens) não. Com timeout=20,
    reenvio do timeout e limiar 1, a 1ª investigação falha (LLMUnavailable) e o disjuntor bloqueia as seguintes."""
    def latency(body):     # MOCK de latência: ~1 s + leitura do contexto + geração (~60 tok/s até max_tokens, raciocínio incluso)
        b = json.loads(body)
        return 1.0 + len(body) / 1000 + b.get("max_tokens", 0) / 60
    t = Transport(latency=latency, default=lambda b: ok(assessment()))
    old = ProxyClient(settings(), None, transport=t, sleeper=lambda _: None, timeout=20, max_retries=1, breaker_threshold=1,
                      breaker_cooldown_s=300, now_fn=Clock())
    assert old.probe()["ok"] is True                                         # llm-check passa
    chat = attach(rt, old)
    d1 = chat.handle("usuario_01", ACCEPT)["dados"]
    assert d1["diagnostico_ia"]["estado"] == "chamada_falhou" and d1["diagnostico_ia"]["tipo_falha"] == "LLMTimeout"
    d2 = chat.handle("usuario_01", ACCEPT)["dados"]
    assert d2["diagnostico_ia"]["estado"] == "indisponivel_disjuntor" and d2["diagnostico_ia"]["disjuntor"]["recupera_em_s"] > 0
    # parâmetros corrigidos (timeout 90 s; timeout não é reenviado) => a mesma investigação usa a IA
    new = ProxyClient(settings(), None, transport=Transport(latency=latency, default=lambda b: ok(assessment())),
                      sleeper=lambda _: None, limits=LLMLimits(), max_retries=1, now_fn=Clock())
    d3 = attach(rt, new).handle("usuario_01", ACCEPT)["dados"]
    assert d3["diagnostico_ia"]["estado"] == "usada" and d3["modelo_ia"] == "usado: gpt-6-astra (validado)"


# ====================================================================== 1. geração simulada com sucesso
def test_successful_generation_records_usage_and_diagnostics(rt):
    cli, t = client(rt.conn, [ok("ok", inp=22, out=5)])
    p = cli.probe()
    assert p["ok"] and p["modelo_servido"] == "gpt-6-astra" and "entrada 22 tok" in p["uso"]
    row = rt.conn.execute("SELECT status, channel, input_tokens, output_tokens, finish_reason FROM llm_usage").fetchone()
    assert tuple(row) == ("ok", "diagnostico", 22, 5, "stop")
    assert t.calls[0]["url"].endswith("/v1/chat/completions") and t.calls[0]["body"]["model"] == "gpt-6-astra"
    d = cli.diagnostics()
    assert d["estado_cliente"] == "pronto" and d["disjuntor"]["estado"] == CLOSED and d["ultima_chamada"]["tentou_chamada"]
    assert no_secret(d) and SECRET not in repr(cli)


def test_gateway_format_variations_are_accepted_but_not_malformed_content():
    cli, _ = client(None, [ok('```json\n{"a": 1}\n```', content_is_list=True)])
    r = cli.chat([{"role": "user", "content": "x"}])
    assert extract_json(r.content) == ({"a": 1}, None)                       # partes em lista + cerca ```json
    assert extract_json('texto {"type": "assessment", "x": [1,2]} fim')[0]["type"] == "assessment"
    assert extract_json('{"type": "assessment", "x": [1,')[0] is None        # truncado: rejeitado
    assert extract_json("")[1] == "resposta vazia"


# ====================================================================== 2. falha transitória + recuperação
def test_transient_failure_then_recovery_without_opening(rt):
    cli, t = client(rt.conn, [HttpResponse(503, {}, b"down"), HttpResponse(502, {}, b""), ok("ok"), ok("ok")])
    with pytest.raises(LLMUnavailable) as e:
        cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")    # 503 + 1 reenvio (502) => uma falha lógica
    assert e.value.http_status == 502 and e.value.attempts == 2 and e.value.kind == "transitoria"
    assert cli.breaker.state == CLOSED and cli.breaker.failures == 1
    assert cli.chat([{"role": "user", "content": "x"}], purpose="chat:x").model == "gpt-6-astra"
    assert cli.breaker.failures == 0 and cli.available


# ====================================================================== 3. máquina de estados do disjuntor
def test_breaker_state_machine_closed_open_half_open_closed():
    clk = Clock()
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    b = CircuitBreaker(conn, threshold=2, cooldown_s=60, permanent_cooldown_s=900, clock=clk)
    assert b.acquire() == (True, None)
    b.on_failure(TRANSIENT, "timeout"); assert b.state == CLOSED
    b.on_failure(TRANSIENT, "timeout"); assert b.state == OPEN and b.cause == "timeout"
    allowed, why = b.acquire()
    assert not allowed and "nova tentativa em 60s" in why                  # sem chamadas repetitivas durante a falha
    clk.t += 61
    assert b.acquire() == (True, None) and b.state == HALF_OPEN
    assert b.acquire()[0] is False                                          # uma única tentativa controlada
    b.on_failure(TRANSIENT, "timeout de novo"); assert b.state == OPEN and b.retry_in_s() == 60
    clk.t += 61
    assert b.acquire()[0] and b.state == HALF_OPEN
    b.on_success(); assert b.state == CLOSED and b.failures == 0 and b.cause is None
    b.on_failure(PERMANENT, "HTTP 403", 403)                                # erro permanente: abre na hora, cooldown longo
    assert b.state == OPEN and b.cur_cooldown == 900 and b.http_status == 403
    snap = b.recover("operador")                                             # recuperação controlada: HALF_OPEN, não CLOSED
    assert snap["estado"] == HALF_OPEN
    log = [tuple(r) for r in conn.execute("SELECT from_state, to_state, actor FROM llm_breaker_log ORDER BY seq")]
    assert log[:2] == [(CLOSED, OPEN, "sistema"), (OPEN, HALF_OPEN, "sistema")] and log[-1] == (OPEN, HALF_OPEN, "operador")


def test_output_rejection_does_not_trip_breaker_but_gateway_html_does():
    cli, _ = client(None, [ok("", finish="length"), ok("", finish="stop"),
                           HttpResponse(200, {}, b"<html>bad gateway</html>"), HttpResponse(200, {}, b"<html>")])
    with pytest.raises(LLMTruncated) as e:
        cli.chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "saida" and "max_tokens" in str(e.value)
    with pytest.raises(LLMMalformed):
        cli.chat([{"role": "user", "content": "x"}])
    assert cli.breaker.state == CLOSED and cli.breaker.failures == 0       # o proxy respondeu: não é indisponibilidade
    for _ in range(2):
        with pytest.raises(LLMMalformed):
            cli.chat([{"role": "user", "content": "x"}])                    # HTML com 200 = gateway: conta
    assert cli.breaker.state == OPEN


# ====================================================================== 4. orçamento esgotado
def test_budget_exhausted_is_distinct_from_breaker(rt):
    lim = LLMLimits(max_calls_per_instance=4, autonomous_share=0.5)
    cli, t = client(rt.conn, [ok("ok")] * 10, limits=lim)
    for _ in range(2):
        cli.chat([{"role": "user", "content": "x"}], purpose="investigacao:R5-X")
    with pytest.raises(LLMBudgetExceeded) as e:                             # cota do autônomo (50%) esgotada
        cli.chat([{"role": "user", "content": "x"}], purpose="investigacao:R5-X")
    assert "cota do agente autônomo" in str(e.value) and len(t.calls) == 2
    assert cli.unavailable_reason(AUTONOMOUS).startswith("orcamento:") and cli.unavailable_reason(INTERACTIVE) is None
    for _ in range(2):
        cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")      # a conversa ainda tem orçamento
    assert cli.unavailable_reason().startswith("orcamento: orçamento da instância esgotado: chamadas 4/4")
    assert cli.breaker.state == CLOSED and cli.diagnostics()["estado_cliente"] == "bloqueado_orcamento"
    rt.llm = cli
    r = AgentChat(rt).handle("usuario_01", ACCEPT)
    g = r["dados"]["diagnostico_ia"]
    assert g["estado"] == "indisponivel_orcamento" and "4/4" in g["motivo_fallback"] and g["orcamento"]["chamadas_restantes"] == 0
    assert "disjuntor aberto ou orçamento esgotado" not in json.dumps(r, ensure_ascii=False)


def test_task_budget_limits_calls_tokens_and_deadline():
    tb = TaskBudget(max_calls=2, max_tokens=1000, deadline_s=100)
    cli, _ = client(None, [ok("ok", inp=700, out=400)] * 3)
    cli.chat([{"role": "user", "content": "x"}], task=tb)
    with pytest.raises(LLMBudgetExceeded) as e:
        cli.chat([{"role": "user", "content": "x"}], task=tb)
    assert "tokens por tarefa" in str(e.value)
    assert TaskBudget(1, 10, 3).blocked_reason().startswith("prazo da tarefa")


# ====================================================================== 5. recuperação após reinicialização
def test_breaker_and_budget_survive_restart_and_recover(rt):
    clk = Clock()
    cli, _ = client(rt.conn, [ok("ok"), TimeoutError("t"), TimeoutError("t")], clock=clk)
    cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")
    for _ in range(2):
        with pytest.raises(LLMTimeout):
            cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")
    assert cli.breaker.state == OPEN
    # "reinício do servidor": novo runtime/cliente sobre o mesmo banco da instância
    rt2 = bootstrap(replace(rt.settings, ai_proxy_api_key=None))
    cli2, t2 = client(rt2.conn, [ok("ok")], clock=clk)
    assert cli2.breaker.state == OPEN and "timeout" in cli2.breaker.cause    # falha não é escondida pelo reinício
    assert cli2.budget.total_calls == 3                                      # consumo persistido (ok + 2 falhas)
    with pytest.raises(LLMCircuitOpen):
        cli2.chat([{"role": "user", "content": "x"}], purpose="chat:x")
    assert t2.calls == []
    clk.t += cli2.limits.breaker_cooldown_s + 1
    assert cli2.chat([{"role": "user", "content": "x"}], purpose="chat:x").model == "gpt-6-astra"
    assert cli2.breaker.state == CLOSED
    rt2.conn.close()


# ====================================================================== 6. resposta malformada do LLM (tarefa)
@pytest.mark.parametrize("content,expect", [
    ("não sei responder em JSON", "sem JSON válido"),
    ('{"type": "assessment", "assessment": "x", "inconsistencies": [{"descricao": "y", "refs": ["E999"]}]}', "sem referência verificável"),
    ('{"type": "approve_everything"}', "contrato inválido"),
])
def test_malformed_model_output_is_rejected_not_treated_as_decision(rt, content, expect):
    cli, _ = client(rt.conn, [ok(content)])
    before = audit_n(rt)
    d = attach(rt, cli).handle("usuario_01", ACCEPT)["dados"]
    g = d["diagnostico_ia"]
    assert g["estado"] == "saida_rejeitada" and expect in g["motivo_fallback"] and g["tentou_chamada"]
    assert d["modelo_ia"].startswith("respondeu, saída rejeitada → fallback determinístico")
    assert all(i["origem"] == "motor determinístico" for i in d["inconsistencias"]) and d["inconsistencias"]
    assert cli.breaker.state == CLOSED and audit_n(rt) == before


# ====================================================================== 7. timeout e rate limit
def test_timeout_is_not_resent_and_rate_limit_waits(rt):
    sleeps = []
    cli, t = client(rt.conn, [TimeoutError("read timed out")], sleeper=sleeps.append)
    with pytest.raises(LLMTimeout) as e:
        cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")
    assert len(t.calls) == 1 and e.value.stage == "durante_chamada" and "não reenviada" in str(e.value)
    cli2, t2 = client(None, [HttpResponse(429, {"retry-after": "3"}, b""), ok("ok")], sleeper=sleeps.append)
    assert cli2.chat([{"role": "user", "content": "x"}]).attempts == 2 and sleeps == [3.0]
    cli3, t3 = client(None, [HttpResponse(429, {"retry-after": "1"}, b"")] * 2, sleeper=sleeps.append)
    with pytest.raises(LLMUnavailable) as e3:
        cli3.chat([{"role": "user", "content": "x"}])
    assert e3.value.http_status == 429


def test_task_deadline_bounds_per_call_timeout():
    cli, t = client(None, [ok("ok")])
    tb = TaskBudget(3, 10_000, deadline_s=30)
    cli.chat([{"role": "user", "content": "x"}], task=tb)
    assert t.calls[0]["timeout"] <= 30 and LLMLimits().timeout_s <= 120


# ====================================================================== 8. HTTP 403
def test_http_403_is_permanent_and_diagnosed_without_secret(rt):
    cli, t = client(rt.conn, [HttpResponse(403, {"server": "cloudflare", "cf-ray": "x"}, f"error code: 1010 {SECRET}".encode())])
    with pytest.raises(LLMForbidden) as e:
        cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")
    assert e.value.kind == "permanente" and e.value.http_status == 403 and SECRET not in str(e.value)
    assert cli.breaker.state == OPEN and cli.breaker.cur_cooldown == LLMLimits().breaker_permanent_cooldown_s
    d = cli.diagnostics()
    assert d["ultima_chamada"]["http_status"] == 403 and d["disjuntor"]["http_status"] == 403 and no_secret(d)
    assert rt.conn.execute("SELECT http_status, error_type FROM llm_usage").fetchone()[:] == (403, "LLMForbidden")


# ====================================================================== 9. isolamento entre instâncias
def test_instances_are_isolated(tmp_path):
    from guardianpay.api.instances import InstanceManager
    base = load_settings(raw_dir=RAW, ai_proxy_api_key=None, llm_mode="off", db_path=tmp_path / "x.db")
    mgr = InstanceManager(base, root=tmp_path / "inst")
    try:
        a, b = mgr.create("A"), mgr.create("B")
        assert a.rt.conn is not b.rt.conn and a.rt.settings.db_path != b.rt.settings.db_path
        for inst in (a, b):          # instância nova = estado independente, sem registros de ações
            assert inst.rt.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
            assert inst.rt.conn.execute("SELECT count(*) FROM chat_messages").fetchone()[0] == 0
            assert inst.rt.conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
        a.step()                     # o agente autônomo de A age no tique 0 (é daí que vêm as "ações anteriores")
        prior = [tuple(r) for r in a.rt.conn.execute(
            "SELECT action, actor FROM audit_log WHERE target='TP431754' AND decision='executed' ORDER BY seq")]
        assert prior and all(actor.startswith("agent:") for _, actor in prior)
        assert b.rt.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
        assert b.rt.conn.execute("SELECT bloqueado FROM op_titulos WHERE titulo_id='TP431754'").fetchone()[0] == 0
        c = mgr.create("C")          # o template nunca recebe ações das instâncias
        assert c.rt.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    finally:
        mgr.shutdown()


def test_breaker_and_budget_are_per_instance(rt, tmp_path):
    other = tmp_path / "other.db"
    src = sqlite3.connect(rt.settings.db_path); dst = sqlite3.connect(other)
    src.backup(dst); src.close(); dst.close()
    rt_b = bootstrap(replace(rt.settings, db_path=other))
    try:
        cli_a, _ = client(rt.conn, [HttpResponse(401, {}, b"bad")])
        cli_b, _ = client(rt_b.conn, [ok("ok")])
        with pytest.raises(Exception):
            cli_a.chat([{"role": "user", "content": "x"}], purpose="chat:x")
        assert cli_a.breaker.state == OPEN and cli_b.breaker.state == CLOSED
        assert cli_b.chat([{"role": "user", "content": "x"}], purpose="chat:x")
        assert cli_a.budget.total_calls == 1 and cli_b.budget.total_calls == 1
    finally:
        rt_b.conn.close()


# ====================================================================== 10. investigação composta usando LLM
def test_composite_investigation_uses_llm_via_real_client(rt):
    seen = []

    def reply(body):
        seen.append(body)
        msgs = body["messages"]
        if len(seen) == 1:      # 1º passo: pede uma leitura adicional (só leitura do catálogo)
            return ok(json.dumps({"type": "read", "requests": [{"tool": "listar_comunicacoes", "params": {"fornecedor_id": "FP4590"}}]}))
        assert "DADO_NAO_CONFIAVEL" in msgs[-1]["content"]
        return ok(assessment(refs=("E1", "E2")), inp=2400, out=350)

    cli, t = client(rt.conn, transport=Transport(default=reply))
    before = audit_n(rt)
    r = attach(rt, cli).handle("usuario_01", ACCEPT)
    d, g = r["dados"], r["dados"]["diagnostico_ia"]
    assert r["intent"] == "tarefa_composta" and d["objetivos"][:1] == ["investigar"]
    assert g["estado"] == "usada" and g["modelo_servido"] == "gpt-6-astra" and g["tentou_chamada"]
    assert g["uso_nesta_tarefa"] == {"chamadas": 2, "tokens_entrada": 3600, "tokens_saida": 650, "modelo": "gpt-6-astra"}
    assert d["modelo_ia"] == "usado: gpt-6-astra (validado)"
    tools = [x["tool"] for x in d["ferramentas_executadas"]]
    assert {"consultar_titulo", "consultar_fornecedor", "motor_de_auditoria", "listar_comunicacoes"} <= set(tools)
    assert "leitura IA: listar_comunicacoes" in d["ferramentas_planejadas"]
    assert d["inconsistencias"][0]["origem"] == "IA (validada)" and set(d["inconsistencias"][0]["refs"]) <= set(d["evidencias"])
    assert d["evidencias"]["E1"]["valor"] is not None
    mut = rt.conn.execute("SELECT count(*) FROM audit_log a WHERE seq>? AND decision IN ('executed','requires_approval') "
                          "AND action IN ('bloquear_titulo','retirar_do_lote','corrigir_lancamento','cancelar_titulo','acionar_pessoa',"
                          "'contatar_fornecedor','abrir_incidente')", (before,)).fetchone()[0]
    assert mut == 0                                                          # nenhuma mutação financeira não solicitada
    assert [r[0] for r in rt.conn.execute("SELECT channel FROM llm_usage")] == ["interativo", "interativo"]
    assert all(c["body"]["max_tokens"] <= LLMLimits().max_tokens_per_call for c in t.calls)
    assert no_secret(d)


# ====================================================================== 11. investigação composta com fallback
def test_composite_investigation_fallback_then_breaker_then_recovery(rt):
    clk = Clock()
    cli, t = client(rt.conn, [TimeoutError("t"), TimeoutError("t")], clock=clk)
    chat = attach(rt, cli)
    d1 = chat.handle("usuario_01", ACCEPT)["dados"]
    g1 = d1["diagnostico_ia"]
    assert g1["estado"] == "chamada_falhou" and g1["tipo_falha"] == "LLMTimeout" and g1["tentativas"] == 1
    assert d1["modelo_ia"] == "falhou → fallback determinístico (LLMTimeout)" and d1["inconsistencias"]
    chat.handle("usuario_01", ACCEPT)
    d3 = chat.handle("usuario_01", ACCEPT)["dados"]
    g3 = d3["diagnostico_ia"]
    assert g3["estado"] == "indisponivel_disjuntor" and g3["disjuntor"]["estado"] == OPEN and "timeout" in g3["motivo_fallback"]
    assert len(t.calls) == 2                                                 # aberto: nenhuma chamada repetitiva
    clk.t += cli.limits.breaker_cooldown_s + 1
    t.default = lambda b: ok(assessment())
    d4 = chat.handle("usuario_01", ACCEPT)["dados"]
    assert d4["diagnostico_ia"]["estado"] == "usada" and cli.breaker.state == CLOSED


# ====================================================================== 12. proposta rejeitada pelo PolicyEngine
@pytest.mark.parametrize("advance,action", [
    (0, {"tool": "contatar_fornecedor", "params": {"fornecedor_id": "FP4590"}, "justification": "x", "evidence_refs": ["E1"]}),
    (27, {"tool": "retirar_do_lote", "params": {"titulo_id": "TP431754", "motivo": "beneficiário divergente"},
          "justification": "conter", "evidence_refs": ["E1"]}),
])
def test_ai_proposal_denied_by_policy_engine(rt, advance, action):
    if advance:
        rt.advance(advance)                                                  # após o corte do lote (03/11 10h)
    cli, _ = client(rt.conn, [ok(assessment(actions=[action]))])
    before = audit_n(rt)
    d = attach(rt, cli).handle("usuario_01", ACCEPT)["dados"]
    p = d["acoes_propostas"][0]
    assert p["resultado"] == "negada pelo PolicyEngine" and p["motivo"]
    assert rt.conn.execute("SELECT count(*) FROM audit_log WHERE seq>? AND action=? AND decision='executed'",
                           (before, action["tool"])).fetchone()[0] == 0


# ====================================================================== 13. nenhum efeito em título não solicitado
def test_no_actions_on_unrequested_titles_with_real_client(rt):
    other = {"tool": "bloquear_titulo", "params": {"titulo_id": "TP426904", "motivo": "aproveitar"}, "justification": "x",
             "evidence_refs": ["E1"]}
    cli, _ = client(rt.conn, [ok(assessment(actions=[other]))])
    snap = {r["titulo_id"]: tuple(r) for r in rt.conn.execute("SELECT * FROM op_titulos WHERE titulo_id<>'TP431754'")}
    before = audit_n(rt)
    d = attach(rt, cli).handle("usuario_01", EXEC)["dados"]
    assert d["acoes_propostas"][0]["resultado"] == "rejeitada"
    assert {r["titulo_id"]: tuple(r) for r in rt.conn.execute("SELECT * FROM op_titulos WHERE titulo_id<>'TP431754'")} == snap
    targets = {r[0] for r in rt.conn.execute("SELECT target FROM audit_log WHERE seq>? AND decision IN ('executed','requires_approval')", (before,))}
    assert {x for x in targets if str(x).startswith("TP")} <= {"TP431754"}


# ====================================================================== cota do agente autônomo (o ciclo não monopoliza)
def test_autonomous_cycle_respects_case_cap_and_channel(rt):
    cli, t = client(rt.conn, transport=Transport(default=lambda b: ok(json.dumps({"type": "plan", "assessment": "ok", "references": ["E1"]}))),
                    limits=LLMLimits(max_llm_cases_per_cycle=2))
    rt.llm = cli; rt.orchestrator.llm = cli
    rt.step()
    notes = [r[0] for r in rt.conn.execute("SELECT llm FROM case_decisions WHERE llm IS NOT NULL AND llm<>'nao_usado'")]
    assert sum(n.startswith("ia:") for n in notes) == 2
    assert any("cota de 2 caso(s) com LLM por ciclo" in n for n in notes)
    assert {r[0] for r in rt.conn.execute("SELECT channel FROM llm_usage")} == {"autonomo"}
    assert rt.conn.execute("SELECT bloqueado FROM op_titulos WHERE titulo_id='TP431754'").fetchone()[0] == 1   # piso determinístico


# ====================================================================== diagnóstico administrativo (API)
def test_llm_diagnostics_and_controlled_recovery_api(tmp_path):
    from fastapi.testclient import TestClient
    from guardianpay.api.app import create_app
    s = replace(load_settings(raw_dir=RAW, ai_proxy_api_key=None, llm_mode="off"), db_path=tmp_path / "x.db", demo_mode=True)
    app = create_app(s, instances_root=tmp_path / "inst", local_hosts={"testclient"})
    c = TestClient(app)
    try:
        op = {"X-GP-Operator": c.get("/api/demo/operator").json()["token"]}
        iid = c.post("/api/instances", json={"name": "diag"}, headers=op).json()["id"]
        h = {"Authorization": "Bearer " + c.post(f"/api/instances/{iid}/auth/demo-login", json={"login": "usuario_01"}, headers=op).json()["token"]}
        assert c.get(f"/api/instances/{iid}/llm/diagnostico", headers=h).json()["estado_cliente"] == "desligado"
        inst = app.state.mgr.get(iid)
        cli, _ = client(inst.rt.conn, [HttpResponse(403, {}, b"nope")])
        inst.rt.llm = cli
        with pytest.raises(LLMForbidden):
            cli.chat([{"role": "user", "content": "x"}], purpose="chat:x")
        d = c.get(f"/api/instances/{iid}/llm/diagnostico", headers=h).json()
        assert d["estado_cliente"] == "bloqueado_disjuntor" and d["disjuntor"]["estado"] == OPEN
        assert d["ultimas_chamadas"][0]["http_status"] == 403 and d["transicoes_disjuntor"][0]["to_state"] == OPEN
        assert no_secret(d)
        assert c.post(f"/api/instances/{iid}/llm/disjuntor/recuperar", headers=h).status_code in (401, 403)   # só operador
        r = c.post(f"/api/instances/{iid}/llm/disjuntor/recuperar", headers=op).json()
        assert r["disjuntor"]["estado"] == HALF_OPEN and r["orcamento"]["chamadas"]["interativo"] == 1        # uso não zerado
    finally:
        app.state.mgr.shutdown()


# ====================================================================== llm-check --tarefa (mesmo caminho do Workspace)
def test_llm_check_task_runs_workspace_path_on_temporary_db(monkeypatch, tmp_path):
    import guardianpay.llm.proxy_client as pc
    from guardianpay.__main__ import _llm_task_check
    t = Transport(default=lambda b: ok(assessment()))
    monkeypatch.setattr(pc, "urllib_transport", t)                         # MOCK: nenhuma rede
    st = replace(settings(), db_path=tmp_path / "nao-usado.db")
    out = _llm_task_check(st, "TP431754")
    assert out["intencao"] == "tarefa_composta" and out["ia"]["estado"] == "usada" and out["mutacoes_financeiras"] == 0
    assert any("motor_de_auditoria" in f for f in out["ferramentas_executadas"]) and out["uso"]
    assert len(t.calls) == 1 and not (tmp_path / "nao-usado.db").exists()   # banco temporário, sem ciclo autônomo
    assert no_secret(out)
