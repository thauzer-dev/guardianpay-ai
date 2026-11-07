"""Correção do Agent Workspace: roteamento híbrido, tarefa composta (investigar → cruzar → avaliar → propor →
rotear → executar → verificar), honestidade sobre o uso da IA e guardas sobre propostas do modelo (MOCKS identificados)."""
import json
from types import SimpleNamespace

import pytest

from guardianpay.api.agent_chat import AgentChat, interpret
from guardianpay.api.composite import is_composite, objectives

ACCEPT = "Investigue o título TP431754 e explique as inconsistências encontradas entre os registros do ERP, fornecedor e DDA."
EXEC = "Investigue o título TP431754, cruze os dados e execute as ações autorizadas"


class FakeLLM:                                   # MOCK: substitui o AI Proxy (sem rede no teste)
    def __init__(self, replies, fail=False):
        self.replies, self.fail, self.available, self.calls = list(replies), fail, True, 0

    def chat(self, messages, **kw):
        self.calls += 1
        if self.fail:
            raise ConnectionError("proxy recusou")
        return SimpleNamespace(content=json.dumps(self.replies.pop(0)), model="mock-gpt-6-astra")


@pytest.fixture
def chat(rt):
    rt.step()
    return AgentChat(rt)


def audit_n(rt):
    return rt.conn.execute("SELECT coalesce(max(seq),0) FROM audit_log").fetchone()[0]


def title(rt, tid):
    return dict(rt.conn.execute("SELECT * FROM op_titulos WHERE titulo_id=?", (tid,)).fetchone())


# ------------------------------------------------------------------ roteamento
def test_routing_rules():
    assert objectives(ACCEPT) == ["investigar", "explicar"] and is_composite(ACCEPT, interpret(ACCEPT)["ids"])
    assert "executar" in objectives(EXEC) and is_composite(EXEC, interpret(EXEC)["ids"])
    # "bloqueado" (particípio) e "encontradas" não são pedidos de execução
    assert objectives("por que o TP431754 foi bloqueado?") == ["investigar"]
    assert "executar" not in objectives(ACCEPT)
    # contenção pura continua determinística (conter_titulo, com guarda de evidência)
    assert not is_composite("reter TP426904", interpret("reter TP426904")["ids"])
    # "ações executadas" só vira consulta de auditoria quando não há entidade
    assert interpret("quais ações você executou?")["intent"] == "acoes_executadas"
    assert interpret("investigue o TP431754 e diga as ações executadas")["intent"] != "acoes_executadas"


def test_simple_title_investigation_stays_deterministic(chat):
    before = audit_n(chat.rt)
    r = chat.handle("usuario_01", "investigar TP431754")
    assert r["intent"] == "investigar_titulo" and "LLM não usado (" in r["interpretacao"]
    assert "fallback" not in r["interpretacao"]                                         # desligado ≠ falha
    assert r["referencias"][0]["tool"] == "consultar_titulo" and "R5_BENEFICIARIO_INCOMPATIVEL" in r["resposta"]
    assert audit_n(chat.rt) == before                                                   # consulta não muta


# ------------------------------------------------------------------ critério de aceite
def test_composite_multi_system_investigation_acceptance(chat):
    before, t0 = audit_n(chat.rt), title(chat.rt, "TP431754")
    r = chat.handle("usuario_01", ACCEPT)
    d = r["dados"]
    assert r["intent"] == "tarefa_composta" and d["tarefa_composta"] and d["titulo"] == "TP431754"
    assert d["objetivos"] == ["investigar", "explicar"]
    tools = [t["tool"] for t in d["ferramentas_executadas"]]
    assert tools[:2] == ["consultar_titulo", "consultar_fornecedor"] and "motor_de_auditoria" in tools
    assert all(t["status"] == "ok" for t in d["ferramentas_executadas"])
    # IA: indicação honesta (sem cliente no teste) — nunca "usado"
    assert d["modelo_ia"].startswith("não usado:") and "IA não usado" in r["interpretacao"]
    srcs = {e["fonte"] for e in d["evidencias"].values()}
    assert any("dda" in s for s in srcs)
    text = " ".join(i["descricao"] for i in d["inconsistencias"])
    assert "61525615000126" in text and "DDA" in text                                    # ERP × fornecedor × DDA
    assert d["acoes_realizadas_agora"] == [] and audit_n(chat.rt) == before              # não pediu execução
    assert d["resultado_verificado"]["estado_atual"]["bloqueado"] == t0["bloqueado"]


def test_expression_acoes_executadas_does_not_divert_main_intent(chat):
    r = chat.handle("usuario_01", "Investigue o TP431754, cruze ERP e DDA e liste as ações executadas pelo agente nele")
    assert r["intent"] == "tarefa_composta" and r["dados"]["titulo"] == "TP431754"
    assert "Ainda não executei nenhuma ação" not in r["resposta"]
    assert r["dados"]["acoes_anteriores_do_agente"]                                       # histórico do título, não global


def test_proxy_failure_explicit_fallback(chat):
    chat.rt.llm = FakeLLM([], fail=True)
    r = chat.handle("usuario_01", ACCEPT)
    d = r["dados"]
    assert d["modelo_ia"] == "falhou → fallback determinístico (ConnectionError)"
    assert "fallback" in r["interpretacao"] and d["inconsistencias"]                      # resultado vem do motor
    assert all(i["origem"] == "motor determinístico" for i in d["inconsistencias"])


def test_llm_used_is_reported_and_reads_are_restricted(chat):
    before = audit_n(chat.rt)
    chat.rt.llm = FakeLLM([
        {"type": "read", "requests": [{"tool": "listar_comunicacoes", "params": {"fornecedor_id": "FP4590"}},
                                      {"tool": "bloquear_titulo", "params": {"titulo_id": "TP431754"}}]},
        {"type": "assessment", "assessment": "Beneficiário do boleto diverge do cadastro e do DDA.",
         "inconsistencies": [{"sistemas": ["ERP", "DDA"], "descricao": "beneficiário divergente", "refs": ["E1"]},
                             {"sistemas": ["ERP"], "descricao": "valor inventado de R$ 1 milhão", "refs": ["E999"]}],
         "proposed_actions": []}])
    r = chat.handle("usuario_01", ACCEPT)
    d = r["dados"]
    assert d["modelo_ia"] == "usado: mock-gpt-6-astra (validado)" and "llm" not in d["avaliacao"].lower()
    assert [i["descricao"] for i in d["inconsistencias"]] == ["beneficiário divergente"]   # sem ref válida → descartada
    assert "bloquear_titulo" not in [t["tool"] for t in d["ferramentas_executadas"]]       # mutação pedida como leitura
    assert audit_n(chat.rt) == before


# ------------------------------------------------------------------ plano de IA com ação proibida
@pytest.mark.parametrize("bad", [
    {"tool": "aprovar_acao", "params": {"solicitacao_id": "X"}, "justification": "auto-aprovar", "evidence_refs": ["E1"]},
    {"tool": "liberar_lote", "params": {"lote_remessa": "RM20261105-MER"}, "justification": "liberar", "evidence_refs": ["E1"]},
    {"tool": "bloquear_titulo", "params": {"titulo_id": "TP426904", "motivo": "outro título"}, "justification": "x", "evidence_refs": ["E1"]},
    {"tool": "bloquear_titulo", "params": {"titulo_id": "TP431754", "motivo": "sem evidência"}, "justification": "x", "evidence_refs": ["E77"]},
])
def test_ai_plan_with_forbidden_action_is_rejected(chat, bad):
    chat.rt.llm = FakeLLM([{"type": "assessment", "assessment": "ok", "inconsistencies": [
        {"sistemas": ["ERP", "DDA"], "descricao": "divergência", "refs": ["E1"]}], "proposed_actions": [bad]}])
    t_before = {t: title(chat.rt, t) for t in ("TP431754", "TP426904")}
    r = chat.handle("usuario_01", EXEC)
    p = r["dados"]["acoes_propostas"][0]
    assert not p["resultado"].startswith("executada"), p
    for tid, t in t_before.items():
        if tid != "TP431754":
            assert title(chat.rt, tid) == t                                               # nada fora do escopo
    assert chat.rt.conn.execute("SELECT count(*) FROM audit_log WHERE action IN ('aprovar_acao','liberar_lote') "
                                "AND decision='executed' AND actor LIKE 'agent:%'").fetchone()[0] == 0


# ------------------------------------------------------------------ execução autorizada, escopo e verificação
def test_execute_request_runs_only_authorized_actions_for_that_title(rt):
    chat = AgentChat(rt)                       # sem ciclo prévio: o agente ainda não agiu sobre o título
    assert title(rt, "TP431754")["bloqueado"] == 0
    before = audit_n(rt)
    r = chat.handle("usuario_01", EXEC)
    d = r["dados"]
    assert "executar" in d["objetivos"]
    acts = d["acoes_realizadas_agora"]
    assert acts and all(a["actor"].startswith("agent:") for a in acts)
    rows = rt.conn.execute("SELECT action, target, payload FROM audit_log WHERE seq>? AND decision IN ('executed','requires_approval')",
                           (before,)).fetchall()
    titles = {r["target"] for r in rows if str(r["target"]).startswith("TP")}
    assert titles <= {"TP431754"}                                                         # escopo: só o título pedido
    assert title(rt, "TP431754")["bloqueado"] == 1
    assert d["resultado_verificado"]["estado_atual"]["bloqueado"] == 1                    # relido após a execução
    assert any(c["case_id"] == "R5-TP431754" for c in d["casos"])
    # repetição: idempotente, sem novas mutações no título
    n = audit_n(rt)
    r2 = chat.handle("usuario_01", EXEC)
    assert not [a for a in r2["dados"]["acoes_realizadas_agora"] if a["decision"] == "executed" and a["action"] == "bloquear_titulo"]
    assert audit_n(rt) >= n


def test_continuity_after_approval(chat):
    rt = chat.rt
    from guardianpay.auth import execute_with_session
    # avança até a correção do R2-TP431459 ficar pendente de aprovação
    for _ in range(12):
        if rt.conn.execute("SELECT count(*) FROM approval_requests WHERE status='pending' AND tool='corrigir_lancamento' "
                           "AND params LIKE '%TP431459%'").fetchone()[0]:
            break
        rt.step()
    r = chat.handle("usuario_01", "Investigue o título TP431459, cruze ERP e NF e execute as ações autorizadas")
    pend = r["dados"]["aprovacoes_pendentes"]
    assert pend and pend[0]["tool"] == "corrigir_lancamento" and title(rt, "TP431459")["valor_cents"] == 4_838_000
    assert "usuario_12" in [c for x in pend[0]["routed_to"] for c in x["candidatos"]]
    tok = rt.sessions.issue_demo("usuario_12", purpose="teste")
    assert execute_with_session(rt, tok, "aprovar_acao", {"solicitacao_id": pend[0]["solicitacao_id"]}).data["status"] == "approved"
    r2 = chat.handle("usuario_01", "Investigue o título TP431459, cruze ERP e NF e execute as ações autorizadas")
    assert title(rt, "TP431459")["valor_cents"] == 483_800
    assert r2["dados"]["resultado_verificado"]["depois"]["valor_cents"] == 483_800
    assert rt.conn.execute("SELECT state FROM cases WHERE case_id='R2-TP431459'").fetchone()[0] == "resolved"
    assert not r2["dados"]["aprovacoes_pendentes"]


def test_execution_is_persistent_across_runtime_restart(chat):
    rt = chat.rt
    chat.handle("usuario_01", EXEC)
    seqs = [r[0] for r in rt.conn.execute("SELECT seq FROM audit_log WHERE action='bloquear_titulo' AND target='TP431754' AND decision='executed'")]
    from guardianpay.runtime import bootstrap
    s = rt.settings
    rt.conn.commit()
    rt2 = bootstrap(s)
    try:
        assert rt2.conn.execute("SELECT bloqueado FROM op_titulos WHERE titulo_id='TP431754'").fetchone()[0] == 1
        assert [r[0] for r in rt2.conn.execute("SELECT seq FROM audit_log WHERE action='bloquear_titulo' AND target='TP431754' "
                                               "AND decision='executed'")] == seqs
        from guardianpay.audit import verify_chain
        assert verify_chain(rt2.conn)[0] is True                                        # cadeia de hash íntegra
        r = AgentChat(rt2).handle("usuario_01", "quais ações você executou?")
        assert "bloquear_titulo" in r["resposta"]
        assert rt2.conn.execute("SELECT count(*) FROM chat_messages WHERE content LIKE ?", (f"%{EXEC[:30]}%",)).fetchone()[0] >= 1
    finally:
        rt2.conn.close()


def test_denied_tools_are_reported_as_denied_not_executed(chat):
    chat.rt.llm = FakeLLM([
        {"type": "read", "requests": [{"tool": "bloquear_titulo", "params": {"titulo_id": "TP431754"}}]},
        {"type": "assessment", "assessment": "ok", "inconsistencies": [{"sistemas": ["ERP"], "descricao": "d", "refs": ["E1"]}]}])
    r = chat.handle("usuario_01", ACCEPT)
    d = r["dados"]
    assert [x["tool"] for x in d["ferramentas_negadas"]] == ["bloquear_titulo"]
    assert "bloquear_titulo" not in [t["tool"] for t in d["ferramentas_executadas"]]
    assert "Ferramentas NEGADAS (não executadas): bloquear_titulo" in r["resposta"]
