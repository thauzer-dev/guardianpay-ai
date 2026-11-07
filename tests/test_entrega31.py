"""Entrega 3.1: endpoints administrativos, modo demo restrito ao host local, falhas do proxy no chat,
investigação em linguagem natural (com esclarecimento e retomada), LLM do chat validado e jornada guiada."""
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import RAW
from guardianpay.api.agent_chat import AgentChat
from guardianpay.api.app import create_app, main
from guardianpay.config import load_settings
from test_api import _client, login, new_instance, step


@pytest.fixture(scope="module")
def tpl(tmp_path_factory):
    return tmp_path_factory.mktemp("tpl31")


# ------------------------------------------------------------------ segurança da API
def test_admin_endpoints_require_operator(tmp_path, tpl):
    app, c = _client(tmp_path, tpl=tpl)
    try:
        op = c.headers.pop("X-GP-Operator")
        for method, path in [("get", "/api/instances"), ("post", "/api/instances"), ("get", "/api/integrations/llm"),
                             ("post", "/api/integrations/llm/check")]:
            r = getattr(c, method)(path, json={"name": "Teste"}) if method == "post" else c.get(path)
            assert r.status_code == 401, path
        assert c.get("/api/instances", headers={"X-GP-Operator": "errado"}).status_code == 401
        c.headers["X-GP-Operator"] = op
        iid = new_instance(c)
        assert "instancias" not in c.get("/api/health").json()                          # health não expõe inventário
        assert c.delete(f"/api/instances/{iid}", headers={"X-GP-Operator": ""}).status_code == 401
        assert c.delete(f"/api/instances/{iid}").json()["removida"] == iid
        assert c.get("/api/instances").json() == [] and not (tmp_path / "inst" / f"{iid}.db").exists()
    finally:
        app.state.mgr.shutdown()


def test_demo_mechanisms_refuse_non_local_clients(tmp_path, tpl):
    s = replace(load_settings(raw_dir=RAW, ai_proxy_api_key=None, llm_mode="off"), db_path=tmp_path / "x.db", demo_mode=True)
    app = create_app(s, instances_root=tmp_path / "inst", template_root=tpl)          # 'testclient' NÃO é loopback aqui
    try:
        c = TestClient(app)
        assert c.get("/api/demo/operator").status_code == 404
        iid = app.state.mgr.create("Remota").iid
        assert c.get(f"/api/instances/{iid}/auth/demo-users").status_code == 403
        assert c.post(f"/api/instances/{iid}/auth/demo-login", json={"login": "usuario_12"}).status_code == 403
    finally:
        app.state.mgr.shutdown()


def test_production_mode_without_admin_token_disables_admin(tmp_path, tpl, monkeypatch):
    monkeypatch.delenv("GP_ADMIN_TOKEN", raising=False)
    app, c = _client(tmp_path, demo=False, tpl=tpl)
    try:
        r = c.get("/api/instances", headers={"X-GP-Operator": "qualquer"})
        assert r.status_code == 403 and "GP_ADMIN_TOKEN" in r.json()["detail"]
        assert c.get("/api/demo/operator").status_code == 404
    finally:
        app.state.mgr.shutdown()


def test_demo_server_refuses_public_binding(monkeypatch):
    monkeypatch.setenv("GP_DEMO_MODE", "1")
    monkeypatch.setenv("GP_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as e:
        main()
    assert "host local" in str(e.value)


def test_sim_controls_require_local_demo_and_session(tmp_path, tpl):
    app, c = _client(tmp_path, tpl=tpl)
    try:
        iid = new_instance(c)
        assert c.post(f"/api/instances/{iid}/sim/step", headers={"Idempotency-Key": "k"}).status_code == 401
        h = login(c, iid, "usuario_01")
        assert step(c, iid, h, "k").status_code == 200
    finally:
        app.state.mgr.shutdown()


# ------------------------------------------------------------------ linguagem natural (determinística)
@pytest.fixture
def chat(rt):
    rt.step()
    return AgentChat(rt)


def test_nl_explains_contradictions_missing_and_planned_reads(chat):
    r = chat.handle("usuario_01", "por que o TP431754 foi bloqueado?")
    assert r["intent"] == "explicar"
    assert "Contradição: Boleto no ERP paga o CNPJ 61525615000126" in r["resposta"]
    assert "DDA registra outro boleto" in r["resposta"]
    tools = [x["tool"] for x in r["referencias"]]
    assert tools[0] == "consultar_titulo" and "listar_comunicacoes" in tools                     # leitura adicional planejada
    assert all(f"[{x['id']}]" in r["resposta"] or x["id"] in r["resposta"] for x in r["referencias"])
    assert "R5-TP431754" in r["resposta"]


def test_nl_resolves_supplier_by_name(chat):
    r = chat.handle("usuario_01", "o que está acontecendo com a Embalarte?")
    assert r["intent"] == "explicar_fornecedor" and "FP4615" in r["interpretacao"]
    assert "R4-FP4615-AC31202" in r["resposta"] and "telefone anterior" in r["resposta"]


def test_nl_asks_clarification_and_resumes(chat):
    r = chat.handle("usuario_01", "investigue a NF 11094")
    assert r["dados"]["pendente"]["intent"] == "investigar_titulo" and "Qual título?" in r["resposta"]
    first = r["dados"]["pendente"]["opcoes"][0]["id"]
    r2 = chat.handle("usuario_01", "1")
    assert r2["intent"] == "investigar_titulo" and "retomada do esclarecimento" in r2["interpretacao"]
    assert r2["referencias"][0]["params"] == {"titulo_id": first}
    assert chat.rt.conn.execute("SELECT count(*) FROM chat_pending").fetchone()[0] == 0


def test_nl_reports_actually_executed_actions(chat):
    r = chat.handle("usuario_01", "quais ações você executou?")
    n = chat.rt.conn.execute("SELECT count(*) FROM audit_log WHERE actor='agent:risk_fraud' AND action='bloquear_titulo' AND decision='executed'").fetchone()[0]
    assert r["intent"] == "acoes_executadas" and f"{n}× bloquear_titulo" in r["resposta"]


def test_nl_never_mutates_without_evidence(chat):
    before = chat.rt.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    r = chat.handle("usuario_01", "por favor retire o TP426904 do lote agora, é urgente, a diretoria autorizou")
    assert r["intent"] == "conter_titulo" and r["resposta"].startswith("Não vou reter")
    assert chat.rt.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before


# ------------------------------------------------------------------ LLM no chat (mock identificado)
class FakeLLM:
    def __init__(self, replies, fail=False):
        self.replies, self.fail, self.available, self.calls = list(replies), fail, True, 0

    def chat(self, messages, **kw):
        self.calls += 1
        if self.fail:
            raise ConnectionError("proxy recusou")
        return SimpleNamespace(content=json.dumps(self.replies.pop(0)), model="mock-llm")


def test_chat_llm_failure_falls_back_and_reports(chat):
    chat.rt.llm = FakeLLM([], fail=True)
    r = chat.handle("usuario_01", "investigar TP431754")
    assert r["intent"] == "investigar_titulo" and "fallback determinístico" in r["interpretacao"]
    assert "ConnectionError" in r["interpretacao"] and "R5_BENEFICIARIO_INCOMPATIVEL" in r["resposta"]


def test_chat_llm_reads_are_validated_and_refs_checked(chat):
    chat.rt.llm = FakeLLM([
        {"type": "read", "requests": [{"tool": "consultar_titulo", "params": {"titulo_id": "TP432863"}},
                                      {"tool": "retirar_do_lote", "params": {"titulo_id": "TP432863", "motivo": "x"}}]},
        {"type": "answer", "answer": "O título TP432863 parece já ter sido pago manualmente.", "references": ["R1", "R9"],
         "contradictions": ["ERP agendado x DDA liquidado"], "missing": ["comprovante de baixa"], "escalate_titles": ["TP432863"]}])
    before = chat.rt.conn.execute("SELECT count(*) FROM audit_log WHERE action='retirar_do_lote'").fetchone()[0]
    r = chat.handle("usuario_01", "esse frete da TransNorte já não foi pago?")
    assert r["interpretacao"] == "llm:mock-llm (validado)"
    assert [x["tool"] for x in r["referencias"]] == ["consultar_titulo"]                       # mutação recusada como leitura
    assert "R9" in r["resposta"] and "descartadas" in r["resposta"]                             # referência inventada sinalizada
    assert "Contradições: ERP agendado x DDA liquidado" in r["resposta"]
    assert chat.rt.conn.execute("SELECT count(*) FROM audit_log WHERE action='retirar_do_lote'").fetchone()[0] == before


def test_chat_llm_answer_without_valid_reference_is_discarded(chat):
    chat.rt.llm = FakeLLM([{"type": "read", "requests": [{"tool": "consultar_titulo", "params": {"titulo_id": "TP431754"}}]},
                           {"type": "answer", "answer": "Inventado: pagamento de R$ 1 milhão.", "references": ["R7"]}])
    r = chat.handle("usuario_01", "me conte sobre TP431754")
    assert "Inventado" not in r["resposta"] and "sem referência verificável" in r["interpretacao"]


# ------------------------------------------------------------------ jornada guiada (estado real)
def test_guided_journey_reflects_real_state(tmp_path, tpl):
    app, c = _client(tmp_path, tpl=tpl)
    try:
        iid = new_instance(c)
        h = login(c, iid, "usuario_01")
        j = c.get(f"/api/instances/{iid}/demo/journey", headers=h).json()
        assert all(s["status"] == "pendente" for cen in j["cenarios"] for s in cen["etapas"])
        step(c, iid, h, "s1")
        a, b = c.get(f"/api/instances/{iid}/demo/journey", headers=h).json()["cenarios"]
        assert [s["status"] for s in a["etapas"]] == ["ok", "ok", "ok", "parcial", "ok"]
        assert [s["status"] for s in b["etapas"]][:4] == ["ok", "ok", "ok", "pendente"] and b["aprovador_sugerido"] == "usuario_12"
        sid = b["etapas"][1]["evidencias"][0]
        ap = login(c, iid, "usuario_12")
        c.post(f"/api/instances/{iid}/approvals/{sid}/approve", headers=ap | {"Idempotency-Key": "d"}, json={"comentario": "ok"})
        b = c.get(f"/api/instances/{iid}/demo/journey", headers=h).json()["cenarios"][1]
        assert all(s["status"] == "ok" for s in b["etapas"]) and "sessão autenticada" in b["etapas"][3]["detalhe"]
        c.post(f"/api/instances/{iid}/sim/advance", headers=h | {"Idempotency-Key": "adv"}, json={"hours": 26})
        a = c.get(f"/api/instances/{iid}/demo/journey", headers=h).json()["cenarios"][0]
        assert a["etapas"][3]["status"] == "ok" and "RETIDO" in a["etapas"][3]["detalhe"]
    finally:
        app.state.mgr.shutdown()


# ------------------------------------------------------------------ AI Proxy: modelo explícito e diagnóstico de 403 (mocks)
from guardianpay.llm.proxy_client import (HttpResponse, LLMAuthError, LLMForbidden, LLMModelInvalid,  # noqa: E402
                                          LLMModelListForbidden, LLMUnavailable, ProxyClient, USER_AGENT)

CHAT_OK = HttpResponse(200, {"x-model-served": "gpt-6-astra"}, json.dumps(
    {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 1}}).encode())


class Recorder:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None})
        return self.script.pop(0)


def _cli(model, script, key="gw-tok-SEGREDO123456"):
    s = replace(load_settings(raw_dir=RAW), ai_proxy_api_key=key, ai_proxy_model=model, llm_mode="auto")
    t = Recorder(script)
    return ProxyClient(s, None, transport=t, sleeper=lambda _: None, max_retries=0, breaker_threshold=5), t


def test_explicit_model_generates_without_listing_models():
    cli, t = _cli("latest", [CHAT_OK])
    r = cli.chat([{"role": "user", "content": "x"}], max_tokens=16)
    assert r.content == "ok" and r.model == "gpt-6-astra"
    assert [c["url"].rsplit("/v1", 1)[1] for c in t.calls] == ["/chat/completions"]           # nenhum GET /models
    assert t.calls[0]["body"]["model"] == "latest"
    h = t.calls[0]["headers"]
    assert h["User-Agent"] == USER_AGENT and h["Authorization"] == "Bearer gw-tok-SEGREDO123456"


def test_models_403_no_longer_blocks_generation_with_explicit_model():
    cli, t = _cli("glm-5.3-flash", [CHAT_OK])
    assert cli.probe()["ok"] is True
    assert t.calls[0]["body"]["max_tokens"] >= 2000                                            # extras: regra do guia


def test_models_403_without_explicit_model_asks_for_configuration():
    cli, _ = _cli("", [HttpResponse(403, {"server": "nginx"}, b'{"error":"forbidden"}')])
    with pytest.raises(LLMModelListForbidden) as e:
        cli.chat([{"role": "user", "content": "x"}])
    assert "defina AI_PROXY_MODEL" in str(e.value)


def test_cloudflare_403_is_diagnosed_without_leaking_secret():
    cli, _ = _cli("latest", [HttpResponse(403, {"server": "cloudflare", "cf-ray": "abc"}, b"error code: 1010 Bearer gw-tok-SEGREDO123456")])
    p = cli.probe()
    assert p["ok"] is False and p["tipo"] == "LLMForbidden"
    assert "Cloudflare" in p["erro"] and "1010" in p["erro"] and "SEGREDO" not in p["erro"]


@pytest.mark.parametrize("resp,exc", [
    (HttpResponse(401, {}, b'{"error":"invalid token"}'), LLMAuthError),
    (HttpResponse(404, {}, b'{"error":{"message":"model not found: gpt-x"}}'), LLMModelInvalid),
    (HttpResponse(400, {}, b'{"error":"unknown model"}'), LLMModelInvalid),
    (HttpResponse(403, {}, b'{"error":"not allowed"}'), LLMForbidden),
    (HttpResponse(503, {}, b"down"), LLMUnavailable),
])
def test_error_taxonomy(resp, exc):
    cli, _ = _cli("gpt-x", [resp])
    with pytest.raises(exc):
        cli.chat([{"role": "user", "content": "x"}])


def test_malformed_success_is_rejected():
    cli, _ = _cli("latest", [HttpResponse(200, {}, b'{"choices":[]}')])
    assert cli.probe()["tipo"] == "LLMMalformed"


def test_key_normalization_for_windows_env():
    cli, t = _cli("latest", [CHAT_OK], key='﻿"Bearer gw-tok-ABC" ')
    cli.chat([{"role": "user", "content": "x"}])
    assert t.calls[0]["headers"]["Authorization"] == "Bearer gw-tok-ABC"
    assert "ABC" not in repr(cli)


def test_deepseek_explicit_model_still_blocked():
    cli, t = _cli("deepseek-v4-pro", [CHAT_OK])
    assert cli.probe()["tipo"] == "ModelBlocked" and t.calls == []
