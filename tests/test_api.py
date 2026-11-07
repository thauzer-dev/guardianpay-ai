"""Entrega 3: API (FastAPI) — autenticação, autorização, isolamento, idempotência, aprovação + retomada,
conversa sem mutação indevida, estado real do proxy e sinal de atualização da interface."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import RAW
from guardianpay.api.app import create_app
from guardianpay.config import load_settings


TPL = {}


def _client(tmp_path, demo=True, tpl=None, **kw):
    s = replace(load_settings(raw_dir=RAW, ai_proxy_api_key=None, llm_mode="off"), db_path=tmp_path / "x.db", demo_mode=demo)
    s = replace(s, **kw) if kw else s
    app = create_app(s, instances_root=tmp_path / "inst", template_root=tpl, local_hosts={"testclient"})
    c = TestClient(app)
    if demo:   # cliente "local" (testclient tratado como loopback) obtém o token de operador de demonstração
        c.headers["X-GP-Operator"] = c.get("/api/demo/operator").json()["token"]
    return app, c


@pytest.fixture(scope="session")
def tpl_root(tmp_path_factory):
    return tmp_path_factory.mktemp("tpl_api")   # ingestão única compartilhada pelos testes de API


@pytest.fixture
def api(tmp_path, tpl_root):
    app, c = _client(tmp_path, tpl=tpl_root)
    yield c
    app.state.mgr.shutdown()


def login(c, iid, user):
    r = c.post(f"/api/instances/{iid}/auth/demo-login", json={"login": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def new_instance(c, name="Teste"):
    r = c.post("/api/instances", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def step(c, iid, h, key):
    return c.post(f"/api/instances/{iid}/sim/step", headers=h | {"Idempotency-Key": key})


def test_requires_server_session(api):
    iid = new_instance(api)
    assert api.get(f"/api/instances/{iid}/overview").status_code == 401
    assert api.get(f"/api/instances/{iid}/overview", headers={"Authorization": "Bearer forjado"}).status_code == 401
    h = login(api, iid, "usuario_01")
    me = api.get(f"/api/instances/{iid}/me", headers=h).json()
    assert me["login"] == "usuario_01" and me["papel"] == "analista_cp" and me["demo"] is True
    assert api.post(f"/api/instances/{iid}/auth/demo-login", json={"login": "agent:orchestrator"}).status_code == 403
    assert api.post(f"/api/instances/{iid}/auth/demo-login", json={"login": "usuario_02"}).status_code == 403   # inativo


def test_demo_login_disabled_outside_demo_mode(tmp_path, tpl_root, monkeypatch):
    monkeypatch.setenv("GP_ADMIN_TOKEN", "op-secreto")
    app, c = _client(tmp_path, demo=False, tpl=tpl_root)
    c.headers["X-GP-Operator"] = "op-secreto"
    try:
        iid = new_instance(c)
        assert c.post(f"/api/instances/{iid}/auth/demo-login", json={"login": "usuario_12"}).status_code == 403
        assert c.post(f"/api/instances/{iid}/auth/sso", json={"id_token": "x"}).status_code == 501
        assert c.post("/api/instances", json={"name": "yy", "approval_simulator": True}).status_code == 403
    finally:
        app.state.mgr.shutdown()


def test_session_isolation_between_instances(api):
    a, b = new_instance(api, "Inst A"), new_instance(api, "Inst B")
    ha = login(api, a, "usuario_12")
    assert api.get(f"/api/instances/{b}/overview", headers=ha).status_code == 401
    step(api, a, ha, "a1")
    hb = login(api, b, "usuario_12")
    assert api.get(f"/api/instances/{b}/overview", headers=hb).json()["acoes_executadas"] == 0   # estado separado
    cmp = api.get(f"/api/compare?a={a}&b={b}", headers=ha | {"X-Compare-Token": hb["Authorization"][7:]})
    assert cmp.status_code == 200 and set(cmp.json()) == {a, b}


def test_sim_commands_idempotent_and_update_signal(api):
    iid = new_instance(api)
    h = login(api, iid, "usuario_01")
    v0 = api.get(f"/api/instances/{iid}/version", headers=h).json()
    assert api.post(f"/api/instances/{iid}/sim/step", headers=h).status_code == 400            # sem Idempotency-Key
    r1 = step(api, iid, h, "k1").json()
    r2 = step(api, iid, h, "k1").json()                                                        # clique duplicado
    assert r1["executado"] and r2.get("replayed") and r1["tick"] == r2["tick"] == 0
    v1 = api.get(f"/api/instances/{iid}/version", headers=h).json()
    assert v1["audit"] > v0["audit"] and v1["transicoes"] > v0["transicoes"]                   # sinal para a UI recarregar
    r3 = step(api, iid, h, "k2").json()
    assert r3["tick"] == 1
    adv = api.post(f"/api/instances/{iid}/sim/advance", headers=h | {"Idempotency-Key": "k3"}, json={"hours": 30}).json()
    assert adv["tick"] == 31
    ov = api.get(f"/api/instances/{iid}/overview", headers=h)
    assert ov.status_code == 200, ov.text
    ov = ov.json()
    assert ov["indicadores"]["pagamentos_processados"]["titulos"] > 0 and any(l["status"] == "enviado" for l in ov["lotes"])


def test_approval_requires_role_and_resumes_agent(api):
    iid = new_instance(api)
    an = login(api, iid, "usuario_01")
    step(api, iid, an, "s1")
    pend = api.get(f"/api/instances/{iid}/approvals", headers=an).json()
    sol = next(p for p in pend if p["tool"] == "corrigir_lancamento" and p["params"]["titulo_id"] == "TP431459")
    assert sol["posso_aprovar"] is False and sol["case_id"] == "R2-TP431459"
    r = api.post(f"/api/instances/{iid}/approvals/{sol['solicitacao_id']}/approve", headers=an | {"Idempotency-Key": "x"}, json={})
    assert r.json()["resultado"]["status"] == "denied"                                         # analista não aprova
    ap = login(api, iid, "usuario_12")
    body = {"comentario": "conferido", "actor_id": "usuario_15"}                               # tentativa de forjar identidade
    r1 = api.post(f"/api/instances/{iid}/approvals/{sol['solicitacao_id']}/approve", headers=ap | {"Idempotency-Key": "d1"}, json=body).json()
    r2 = api.post(f"/api/instances/{iid}/approvals/{sol['solicitacao_id']}/approve", headers=ap | {"Idempotency-Key": "d1"}, json=body).json()
    assert r1["resultado"]["status"] == "ok" and r2.get("replayed")
    assert r1["caso"] == {"case_id": "R2-TP431459", "state": "resolved"} and r1["agente_retomado"]["acoes"] >= 2
    case = api.get(f"/api/instances/{iid}/cases/R2-TP431459", headers=ap).json()
    acts = [(a["actor"], a["action"], a["decision"]) for a in case["acoes"]]
    assert ("usuario_12", "aprovar_acao", "executed") in acts
    assert ("agent:orchestrator", "corrigir_lancamento", "executed") in acts
    assert [t["to_state"] for t in case["transicoes"]][-1] == "resolved"
    assert next(t for t in case["titulos"] if t["titulo_id"] == "TP431459")["valor_cents"] == 483_800


def test_chat_uses_real_tools_and_never_mutates_without_evidence(api):
    iid = new_instance(api)
    h = login(api, iid, "usuario_01")
    step(api, iid, h, "s1")
    before = api.get(f"/api/instances/{iid}/version", headers=h).json()["audit"]
    r = api.post(f"/api/instances/{iid}/agent/chat", headers=h, json={"message": "investigar TP431754"}).json()
    assert r["intent"] == "investigar_titulo" and r["interpretacao"].startswith("deterministica")
    assert "LLM não usado (GP_LLM_MODE=off)" in r["interpretacao"]          # nunca silencioso sobre o uso da IA
    assert [x["tool"] for x in r["referencias"]][:2] == ["consultar_titulo", "consultar_fornecedor"]
    assert "R5_BENEFICIARIO_INCOMPATIVEL" in r["resposta"] and "BLOQUEADO" in r["resposta"]
    r = api.post(f"/api/instances/{iid}/agent/chat", headers=h, json={"message": "reter TP426904"}).json()
    assert r["intent"] == "conter_titulo" and r["resposta"].startswith("Não vou reter")
    assert api.get(f"/api/instances/{iid}/version", headers=h).json()["audit"] == before          # nenhuma mutação
    r = api.post(f"/api/instances/{iid}/agent/chat", headers=h, json={"message": "caso R9-NAOEXISTE"}).json()
    assert "não existe" in r["resposta"]
    hist = api.get(f"/api/instances/{iid}/agent/messages", headers=h).json()
    assert len(hist) == 6 and hist[0]["actor_id"] == "usuario_01"


def test_llm_integration_reports_real_failure(api, tmp_path, monkeypatch):
    r = api.post("/api/integrations/llm/check").json()                     # sem chave: estado real "desligado"
    assert r["ultima_verificacao"]["ok"] is False and "desligado" in r["ultima_verificacao"]["error"]
    from guardianpay.api import app as appmod

    class Boom:                                                            # MOCK identificado: proxy recusando conexão
        def __init__(self, *a, **k): pass
        def list_models(self):
            raise RuntimeError("HTTP 403 tunnel")
    monkeypatch.setattr(appmod, "ProxyClient", Boom)
    s = replace(load_settings(raw_dir=RAW, ai_proxy_api_key="gw-tok-segredo", llm_mode="auto"), db_path=tmp_path / "y.db", demo_mode=True)
    app2 = create_app(s, instances_root=tmp_path / "inst2", local_hosts={"testclient"})
    try:
        c2 = TestClient(app2)
        assert c2.post("/api/integrations/llm/check").status_code == 401          # exige operador
        c2.headers["X-GP-Operator"] = c2.get("/api/demo/operator").json()["token"]
        out = c2.post("/api/integrations/llm/check").json()
        assert out["configurado"] is True and out["ultima_verificacao"]["ok"] is False
        assert "403" in out["ultima_verificacao"]["error"] and "gw-tok-segredo" not in json.dumps(out)
    finally:
        app2.state.mgr.shutdown()


def test_reset_recreates_state_and_invalidates_sessions(api):
    iid = new_instance(api)
    h = login(api, iid, "usuario_01")
    step(api, iid, h, "s1")
    assert api.post(f"/api/instances/{iid}/sim/reset", headers=h | {"Idempotency-Key": "r1"}).status_code == 200
    assert api.get(f"/api/instances/{iid}/overview", headers=h).status_code == 401              # sessão antiga inválida
    h2 = login(api, iid, "usuario_01")
    ov = api.get(f"/api/instances/{iid}/overview", headers=h2).json()
    assert ov["acoes_executadas"] == 0 and ov["agente"]["tick"] == 0


def test_server_side_runner_operates_without_ui(api):
    import time
    iid = new_instance(api)
    h = login(api, iid, "usuario_01")
    api.post(f"/api/instances/{iid}/sim/resume", headers=h | {"Idempotency-Key": "go"}, json={"interval_s": 0.2})
    deadline = time.time() + 15
    while time.time() < deadline and api.app.state.mgr.get(iid).rt.clock.tick < 3:
        time.sleep(0.2)
    api.post(f"/api/instances/{iid}/sim/pause", headers=h | {"Idempotency-Key": "stop"})
    assert api.app.state.mgr.get(iid).rt.clock.tick >= 3                                       # avançou sem chamadas da UI
