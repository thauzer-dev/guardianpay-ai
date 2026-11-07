from guardianpay.tools.contract import ToolRequest, ToolStatus

EV = [{"source": "n_titulos", "ref": "TP426904"}]


def run(rt, tool, actor, approval=None, key=None, **params):
    return rt.executor.execute(ToolRequest(tool=tool, actor_id=actor, params=params, approval_id=approval, idempotency_key=key))


def test_unknown_tool_actor_and_inactive_user(rt):
    assert run(rt, "pagar_agora", "agent:orchestrator").status is ToolStatus.denied
    assert run(rt, "consultar_titulo", "hacker", titulo_id="TP426904").status is ToolStatus.denied
    r = run(rt, "retirar_do_lote", "usuario_02", titulo_id="TP426904", motivo="x")
    assert r.status is ToolStatus.denied and "inativo" in r.reasons[0]


def test_agent_cannot_approve_or_release(rt):
    for tool, p in [("aprovar_acao", {"solicitacao_id": "SOL-X"}), ("liberar_lote", {"lote_remessa": "RM20261103-MER"})]:
        r = run(rt, tool, "agent:orchestrator", **p)
        assert r.status is ToolStatus.denied


def test_role_from_registry_not_request(rt):
    r = rt.executor.execute(ToolRequest(tool="bloquear_titulo", actor_id="agent:orchestrator",
                                        params={"titulo_id": "TP426904", "motivo": "x"}))
    assert r.status is ToolStatus.denied        # analista_cp não bloqueia; só auditor
    r = run(rt, "bloquear_titulo", "agent:risk_fraud", titulo_id="TP426904", motivo="suspeita")
    assert r.status is ToolStatus.ok


def test_param_validation_and_evidence(rt):
    assert run(rt, "cancelar_titulo", "agent:orchestrator", titulo_id="TP426904").status is ToolStatus.denied
    r = run(rt, "cancelar_titulo", "agent:orchestrator", titulo_id="TP426904", motivo="dup", evidencias=["texto solto"])
    assert r.status is ToolStatus.denied and "evidências" in r.reasons[0]
    r = run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426904", motivo="x", role="aprovador")
    assert r.status is ToolStatus.denied and "desconhecidos" in r.reasons[0]


def test_reversible_retirar_allowed_without_approval(rt):
    r = run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426904", motivo="investigação")
    assert r.status is ToolStatus.ok
    t = rt.conn.execute("SELECT status, lote_remessa FROM op_titulos WHERE titulo_id='TP426904'").fetchone()
    assert (t["status"], t["lote_remessa"]) == ("aberto", "")
    # original intacto
    assert rt.conn.execute("SELECT status FROM raw_contas_a_pagar WHERE titulo_id='TP426904'").fetchone()[0] == "agendado"


def test_alcada_aprovador_and_diretora(rt):
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426988", motivo="x")      # 15.264,46
    r = run(rt, "agendar_titulo", "agent:orchestrator", titulo_id="TP426988", lote_remessa="RM20261103-MER")
    assert r.status is ToolStatus.pending_approval and r.data["required"] == [{"role": "aprovador"}]
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP430403", motivo="x")      # 63.198,60
    r = run(rt, "agendar_titulo", "agent:orchestrator", titulo_id="TP430403", lote_remessa="RM20261110-MER")
    assert r.status is ToolStatus.pending_approval and r.data["required"] == [{"pessoa_id": "PS03"}]


def test_antecipacao_requires_dual(rt):
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426988", motivo="x")
    rt.conn.execute("UPDATE op_titulos SET valor_cents=100000 WHERE titulo_id='TP426988'")  # cabe na alçada
    r = run(rt, "agendar_titulo", "agent:orchestrator", titulo_id="TP426988", lote_remessa="RM20261103-MER")
    assert r.status is ToolStatus.ok          # vencimento 03/11 = data do lote: não é antecipação
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426988", motivo="y")
    rt.conn.execute("UPDATE op_titulos SET vencimento='2026-11-20' WHERE titulo_id='TP426988'")
    r = run(rt, "agendar_titulo", "agent:orchestrator", titulo_id="TP426988", lote_remessa="RM20261103-MER", key="k2")
    assert r.status is ToolStatus.pending_approval
    assert r.data["required"] == [{"role": "aprovador"}, {"pessoa_id": "PS01"}]


def test_fracionamento_aggregates_same_supplier_nf_day(rt):
    # TP428585 (2.506,76) já está no lote de 04/11; TP428586 tem a mesma NF e o mesmo valor
    rt.conn.execute("UPDATE op_titulos SET vencimento='2026-11-04' WHERE titulo_id='TP428586'")
    r = run(rt, "agendar_titulo", "agent:orchestrator", titulo_id="TP428586", lote_remessa="RM20261104-MER")
    assert r.status is ToolStatus.pending_approval
    assert any("soma" in x for x in r.reasons)


def test_blocked_title_cannot_be_scheduled(rt):
    run(rt, "bloquear_titulo", "agent:risk_fraud", titulo_id="TP426904", motivo="suspeita")
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426904", motivo="x")
    r = run(rt, "agendar_titulo", "agent:orchestrator", titulo_id="TP426904", lote_remessa="RM20261103-MER")
    assert r.status is ToolStatus.denied and "bloqueado" in r.reasons[0]


def test_dual_approval_flow_and_segregation(rt):
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426988", motivo="x")
    rt.conn.execute("UPDATE op_titulos SET vencimento='2026-11-20' WHERE titulo_id='TP426988'")
    params = {"titulo_id": "TP426988", "lote_remessa": "RM20261103-MER"}
    pend = run(rt, "agendar_titulo", "agent:orchestrator", **params)
    sid = pend.solicitacao_id
    # sem aprovação: execução com approval_id é negada
    assert run(rt, "agendar_titulo", "agent:orchestrator", approval=sid, **params).status is ToolStatus.denied
    # aprovador não-PS01 aprova (cobre papel aprovador)
    a1 = run(rt, "aprovar_acao", "usuario_12", solicitacao_id=sid)
    assert a1.status is ToolStatus.ok and a1.data["status"] == "pending"
    # mesmo aprovador de novo: negado
    assert run(rt, "aprovar_acao", "usuario_12", solicitacao_id=sid, comentario="de novo").status is ToolStatus.denied
    # tesoureiro não é PS01: não cobre requisito restante
    assert run(rt, "aprovar_acao", "usuario_14", solicitacao_id=sid).status is ToolStatus.denied
    # PS01 completa
    a2 = run(rt, "aprovar_acao", "usuario_13", solicitacao_id=sid)
    assert a2.data["status"] == "approved"
    # parâmetros adulterados: negado
    bad = run(rt, "agendar_titulo", "agent:orchestrator", approval=sid, titulo_id="TP426988", lote_remessa="RM20261104-MER")
    assert bad.status is ToolStatus.denied
    ok = run(rt, "agendar_titulo", "agent:orchestrator", approval=sid, **params)
    assert ok.status is ToolStatus.ok
    # aprovação já consumida não pode ser reutilizada em outra execução
    run(rt, "retirar_do_lote", "agent:orchestrator", titulo_id="TP426988", motivo="z", key="kz")
    again = run(rt, "agendar_titulo", "agent:orchestrator", approval=sid, key="outra", **params)
    assert again.status is ToolStatus.denied


def test_requester_cannot_approve_own_request(rt):
    run(rt, "retirar_do_lote", "usuario_13", titulo_id="TP426988", motivo="x")
    pend = run(rt, "agendar_titulo", "usuario_01", titulo_id="TP426988", lote_remessa="RM20261103-MER")
    rt.conn.execute("UPDATE approval_requests SET requested_by='usuario_12' WHERE solicitacao_id=?", (pend.solicitacao_id,))
    r = run(rt, "aprovar_acao", "usuario_12", solicitacao_id=pend.solicitacao_id)
    assert r.status is ToolStatus.denied and "segregação" in r.reasons[0]


def test_supplier_contact_only_via_registered_channel(rt):
    r = run(rt, "contatar_fornecedor", "agent:orchestrator", fornecedor_id="FP6026", canal="email", mensagem="Confirma NF?")
    reg = rt.conn.execute("SELECT email_cadastrado FROM op_fornecedores WHERE fornecedor_id='FP6026'").fetchone()[0]
    assert r.status is ToolStatus.ok and r.data["destino"] == reg and r.provenance == "simulado"
    r = run(rt, "contatar_fornecedor", "agent:orchestrator", fornecedor_id="FP6026", canal="whatsapp", mensagem="x")
    assert r.status is ToolStatus.failed
