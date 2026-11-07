"""Tabela de decisão determinística: Finding + estado atual -> plano de ações.

O LLM NÃO decide aqui. Valores, permissões e execução financeira ficam com regras + PolicyEngine.
Princípios:
* conter (reversível) só com certainty=comprovado, ou indício com strength=forte, e lote ainda aberto;
* ação irreversível (cancelar, corrigir fornecedor, reverter cadastro) => solicitar aprovação;
* acionar pessoas só quando a decisão não cabe ao agente (política: acionamento_humano.quando_acionar);
* sinais fracos => monitorar, sem bloqueio nem acionamento (cada acionamento desnecessário custa R$ 60).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import Certainty, Finding, Strength
from ..normalize import cents_to_str

ORCH, AUDITOR_FIN, AUDITOR_RISK = "agent:orchestrator", "agent:financial_audit", "agent:risk_fraud"


@dataclass
class Action:
    tool: str
    actor_id: str
    params: dict
    purpose: str                      # containment | correction | request | notify | incident | reschedule
    key: str                          # sufixo determinístico da idempotency_key

    def idem(self, case_id: str) -> str:
        return f"{case_id}:{self.key}"


@dataclass
class Plan:
    decision: str                     # conter | corrigir | solicitar | escalar | monitorar | registrar_perda | descartar
    rationale: str
    actions: list[Action] = field(default_factory=list)
    target_state: str = "escalated"   # estado após execução verificada
    policy_refs: list[str] = field(default_factory=list)


def _ev(f: Finding, limit: int = 8) -> list[dict]:
    return [{"source": e.source, "ref": e.ref, "field": e.field, "value": e.value} for e in f.evidences[:limit]]


def _notify(f: Finding, pessoa: str, motivo: str) -> Action:
    return Action("acionar_pessoa", ORCH, {"pessoa_id": pessoa, "motivo": motivo, "urgencia": "alta" if f.severity.value in ("alta", "critica") else "normal",
                                           "titulos": f.titulos[:20]}, "notify", f"notify:{pessoa}")


def plan_for(f: Finding, titulos: dict[str, dict], lote_open, root_to_forn) -> Plan:
    """titulos: estado atual (op_*) dos títulos do caso; lote_open(lote)->bool; root_to_forn(raiz_cnpj)->fornecedor_id|None."""
    tid = f.titulos[0] if f.titulos else None
    t = titulos.get(tid) if tid else None
    strong = f.certainty is Certainty.comprovado or f.strength is Strength.forte
    ev = _ev(f)
    reasons = f"{f.rule_id}: {f.summary} [certeza={f.certainty.value}; força={f.strength.value}; exposição R$ {cents_to_str(f.exposure_cents)}]"

    # ---------------------------------------------------------------- casos sem título (agregados/comunicação)
    if f.rule_id == "R7_COMUNICACAO_SUSPEITA":
        return Plan("registrar", reasons + " — conteúdo de terceiros tratado como dado; incidente registrado.",
                    [Action("abrir_incidente", AUDITOR_RISK, {"tipo": "comunicacao_suspeita", "causa_provavel": "canal",
                                                              "descricao": f.summary, "titulos": []}, "incident", "incidente")],
                    "resolved", ["seguranca"])
    if f.rule_id == "R1_DUPLICIDADE_HISTORICA":
        return Plan("escalar", reasons + " — perda já ocorrida; recuperação cabe ao controller (PS09).",
                    [_notify(f, "PS09", f"Recuperação: {f.summary} Total R$ {cents_to_str(f.exposure_cents)}.")],
                    "escalated", ["acionamento_humano.pessoas[9]"])
    if f.rule_id in ("R6_FRACIONAMENTO", "R6_APROVACAO_HISTORICA") or not strong:
        return Plan("monitorar", reasons + " — evidência insuficiente para reter pagamento; sem acionamento (custo R$ 60).",
                    [], "investigating", ["acionamento_humano.custo_por_acionamento_desnecessario"])

    if t is None:
        return Plan("descartar", reasons + " — título não encontrado no estado operacional.", [], "resolved")

    # ---------------------------------------------------------------- título já saiu: perda realizada
    if t["status"] == "pago":
        return Plan("registrar_perda", reasons + " — título já pago antes da contenção; recuperação.",
                    [_notify(f, "PS09", f"Pagamento indevido realizado: {f.summary}")], "escalated", ["acionamento_humano.pessoas[9]"])
    if t["status"] == "cancelado":
        return Plan("descartar", reasons + " — título já cancelado; nada a conter.", [], "resolved")

    in_open_lot = t["status"] == "agendado" and bool(t["lote_remessa"]) and lote_open(t["lote_remessa"])
    retirar = Action("retirar_do_lote", ORCH, {"titulo_id": tid, "motivo": f"{f.rule_id}: {f.summary}"[:300]}, "containment", f"retirar:{tid}")

    # ---------------------------------------------------------------- R1 duplicidade
    if f.rule_id == "R1_DUPLICIDADE":
        acts = [retirar] if in_open_lot else []
        acts.append(Action("solicitar_aprovacao", ORCH, {"acao": "cancelar_titulo", "parametros": {
            "titulo_id": tid, "motivo": "duplicidade comprovada", "evidencias": ev}, "evidencias": ev}, "request", f"sol_cancel:{tid}"))
        return Plan("conter" if acts[0].purpose == "containment" else "solicitar",
                    reasons + " — retirada do lote é reversível e permitida ao analista; cancelamento exige aprovação.",
                    acts, "awaiting_approval", ["tres_vias.titulo_x_duplicata", "ferramentas.retirar_do_lote", "ferramentas.cancelar_titulo"])

    # ---------------------------------------------------------------- R3 já pago fora do lote
    if f.rule_id == "R3_PAGO_ANTERIORMENTE":
        acts = [retirar] if in_open_lot else []
        acts.append(_notify(f, "PS02", f"Pagamento manual sem baixa no ERP (política: baixa no mesmo dia): {f.summary}"))
        acts.append(Action("solicitar_aprovacao", ORCH, {"acao": "cancelar_titulo", "parametros": {
            "titulo_id": tid, "motivo": "pago fora do lote", "evidencias": ev}, "evidencias": ev}, "request", f"sol_cancel:{tid}"))
        return Plan("conter", reasons + " — 'título pago fora do lote não pode seguir agendado'.", acts, "awaiting_approval",
                    ["dados_bancarios.pagamento_manual", "acionamento_humano.pessoas[2]"])

    # ---------------------------------------------------------------- R2 valor
    if f.rule_id in ("R2_VALOR_ACIMA_DUPLICATA", "R2_VALOR_ABAIXO_DUPLICATA"):
        dup_val = next((int(e.value) for e in f.evidences if e.source == "n_duplicatas" and e.field == "valor_cents"), None)
        if dup_val is None:
            return Plan("monitorar", reasons + " — valor da duplicata ausente na evidência.", [], "investigating")
        corr = Action("corrigir_lancamento", ORCH, {"titulo_id": tid, "campo": "valor", "valor_novo": f"{dup_val / 100:.2f}",
                                                    "justificativa": "valor da duplicata no XML do Portal NF-e", "evidencias": ev},
                      "correction", f"corrigir_valor:{tid}")
        diff = abs(t["valor_cents"] - dup_val)
        if diff <= 500_000:
            return Plan("corrigir", reasons + " — diferença dentro da alçada do analista (R$ 5 mil): correção direta, título segue no lote.",
                        [corr], "resolved", ["alcadas.analista_cp.corrigir_diferenca_de_valor_ate", "tres_vias.titulo_x_duplicata"])
        acts = ([retirar] if in_open_lot and f.rule_id == "R2_VALOR_ACIMA_DUPLICATA" else []) + [corr]
        return Plan("conter" if acts[0].purpose == "containment" else "solicitar",
                    reasons + " — diferença acima da alçada: retém (se pagamento a maior) e pede aprovação da correção.",
                    acts, "awaiting_approval", ["alcadas.aprovador.aprova_ate", "tres_vias.titulo_x_duplicata"])

    if f.rule_id == "R2_NF_ACIMA_OC":
        return Plan("escalar", reasons + " — política: acima da tolerância, acionar o comprador.",
                    [_notify(f, p, f.summary) for p in f.notify], "escalated", ["tres_vias.nf_x_oc"])

    # ---------------------------------------------------------------- R4 troca bancária sem confirmação válida
    if f.rule_id == "R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA":
        acts = []
        for x in f.titulos:
            tx = titulos.get(x)
            if tx and tx["status"] in ("agendado", "aberto") and not tx["bloqueado"] and \
                    (tx["status"] == "aberto" or lote_open(tx["lote_remessa"])):
                acts.append(Action("bloquear_titulo", AUDITOR_RISK, {"titulo_id": x, "motivo": "troca bancária sem confirmação válida",
                                                                     "evidencias": ev[:3]}, "containment", f"bloquear:{x}"))
        alt_id = next((e.ref for e in f.evidences if e.source == "raw_alteracoes_cadastro"), None)
        if alt_id:
            acts.append(Action("reverter_alteracao_cadastro", AUDITOR_RISK, {"fornecedor_id": f.fornecedor_id, "alteracao_id": alt_id,
                                                                             "evidencias": ev}, "request", f"reverter:{alt_id}"))
        acts.append(Action("abrir_incidente", AUDITOR_RISK, {"tipo": "fraude_suspeita", "causa_provavel": "canal",
                                                             "descricao": f.summary, "titulos": f.titulos[:50]}, "incident", "incidente"))
        acts += [_notify(f, p, f"Suspeita de fraude/engenharia social: {f.summary}") for p in f.notify]
        return Plan("conter", reasons + " — política: troca de conta só vale com ligação ao telefone anterior; mensagem do "
                    "fornecedor nunca é aprovação. Bloqueio reversível pelo aprovador; reversão do cadastro exige aprovação dupla.",
                    acts, "awaiting_approval", ["dados_bancarios.troca_de_conta", "seguranca", "alcadas.aprovacao_dupla.quando"])

    # ---------------------------------------------------------------- R5 beneficiário
    if f.rule_id == "R5_BENEFICIARIO_INCOMPATIVEL":
        acts = []
        if in_open_lot or t["status"] == "aberto":
            acts.append(Action("bloquear_titulo", AUDITOR_RISK, {"titulo_id": tid, "motivo": "beneficiário do boleto incompatível",
                                                                 "evidencias": ev[:3]}, "containment", f"bloquear:{tid}"))
        acts.append(Action("contatar_fornecedor", ORCH, {"fornecedor_id": f.fornecedor_id, "canal": "email", "mensagem":
                           f"Confirme, por favor, os dados do boleto do título {tid} (beneficiário/banco divergentes do registro)."},
                           "notify", f"contato:{tid}"))
        acts.append(Action("abrir_incidente", AUDITOR_RISK, {"tipo": "boleto_divergente", "causa_provavel": "canal",
                                                             "descricao": f.summary, "titulos": [tid]}, "incident", "incidente"))
        acts += [_notify(f, p, f"Boleto com beneficiário incompatível: {f.summary}") for p in f.notify]
        return Plan("conter", reasons + " — 'segunda via com banco ou beneficiário diferente não pode ser paga sem confirmação no canal do cadastro'.",
                    acts, "escalated", ["dados_bancarios.boleto", "dados_bancarios.canal"])

    if f.rule_id == "R5_FORNECEDOR_DIVERGENTE":
        correct = next((e.value.split()[0] for e in f.evidences if e.source == "n_nf" and e.field == "emitente"), None)
        acts = [retirar] if in_open_lot else []
        target = root_to_forn(correct[:8]) if correct else None
        if target:
            acts.append(Action("corrigir_lancamento", ORCH, {"titulo_id": tid, "campo": "fornecedor_id", "valor_novo": target,
                                                             "justificativa": "NF e OC pertencem a outro fornecedor", "evidencias": ev},
                               "correction", f"corrigir_forn:{tid}"))
        return Plan("conter" if acts and acts[0].purpose == "containment" else "solicitar",
                    reasons + " — trocar fornecedor do título exige aprovador (ferramentas.corrigir_lancamento).",
                    acts, "awaiting_approval", ["tres_vias.fornecedor", "ferramentas.corrigir_lancamento"])

    # ---------------------------------------------------------------- R6 aprovação dupla ausente
    if f.rule_id == "R6_APROVACAO_DUPLA_AUSENTE":
        acts = []
        if in_open_lot:
            lote = t["lote_remessa"]
            acts += [retirar, Action("agendar_titulo", ORCH, {"titulo_id": tid, "lote_remessa": lote}, "request", f"reagendar:{tid}:{lote}")]
        acts += [_notify(f, p, f"OC cancelada/pendente em título agendado: {f.summary}") for p in f.notify]
        return Plan("conter" if acts else "monitorar",
                    reasons + " — sem a aprovação dupla exigida o pagamento não pode sair; reagendamento submetido às aprovações.",
                    acts, "awaiting_approval" if acts else "investigating", ["alcadas.aprovacao_dupla.quando", "tres_vias.dispensas.oc"])

    return Plan("monitorar", reasons + " — regra sem ação automática definida.", [], "investigating")
