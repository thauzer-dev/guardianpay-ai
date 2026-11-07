"""Regras determinísticas de auditoria. Cada regra devolve `Finding`s com evidências rastreáveis.

Distinção obrigatória:
* certainty=comprovado  -> a irregularidade/violação de política está demonstrada por registros (ex.: mesma
  duplicata lançada duas vezes; alteração bancária sem confirmação registrada).
* certainty=indicio     -> sinal que exige investigação/humano (ex.: débito avulso no extrato sem vínculo).
* strength (fraca/moderada/forte) é qualitativa e NÃO é probabilidade calibrada.

Exceções legítimas aplicadas: filiais (raiz do CNPJ), parcelas, arredondamento de R$ 0,10, multa 2% + juros
1% a.m. pro rata quando o pagamento ocorre após o vencimento da duplicata, vencimento no próximo dia útil,
utilidades sem OC, aluguel sem NF, banco de cobrança historicamente usado pelo fornecedor.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from ..domain.models import Certainty, Evidence, Finding, Provenance, Severity, Strength
from ..normalize import cents_to_str
from .context import BANK_FIELDS, AuditContext

ROUNDING_TOL = 10            # R$ 0,10
SMALL_DIFF_NO_CONTAIN = 1000  # até R$ 10,00 de excesso: corrigir sem reter (custo de atraso > benefício)
OPEN = ("agendado", "aberto")


def _sev(exposure: int, floor: Severity = Severity.baixa) -> Severity:
    order = [Severity.baixa, Severity.media, Severity.alta, Severity.critica]
    s = Severity.critica if exposure >= 5_000_000 else Severity.alta if exposure >= 1_000_000 else \
        Severity.media if exposure >= 100_000 else Severity.baixa
    return max(s, floor, key=order.index)


def _ev(source, ref, field=None, value=None, prov=Provenance.original) -> Evidence:
    return Evidence(source=source, ref=str(ref), field=field, value=None if value is None else str(value), provenance=prov)


def _t_ev(t: dict, *fields) -> list[Evidence]:
    return [_ev("op_titulos", t["titulo_id"], f, t[f]) for f in fields]


def _late_fee_max(dup_val: int, dup_venc: str, pay_date: str) -> int:
    """Teto legítimo do título pago após o vencimento da duplicata (multa 2% + juros 1% a.m. pro rata)."""
    late = (date.fromisoformat(pay_date) - date.fromisoformat(dup_venc)).days
    if late <= 0:
        return dup_val + ROUNDING_TOL
    return int(dup_val * (1 + 0.02 + 0.01 * late / 30)) + ROUNDING_TOL


def _pay_date(ctx: AuditContext, t: dict) -> str:
    lote = ctx.lotes.get(t["lote_remessa"])
    return lote["data_prevista"] if lote else t["vencimento"]


# ====================================================================== R1 — duplicidade
def r1_duplicidade(ctx: AuditContext) -> list[Finding]:
    out: list[Finding] = []
    recovery: list[tuple[dict, dict]] = []
    for key, group in ctx.by_parcel.items():
        live = [t for t in group if t["status"] != "cancelado"]
        if len(live) < 2:
            continue
        root, nf_num, pn, pt = key
        nf_cands = ctx.nfs.get((root, nf_num), [])
        sufixos = {t["nf_sufixo"] for t in live if t["nf_sufixo"]}
        if len(nf_cands) > 1 and len(sufixos) > 1:
            continue  # notas distintas com o mesmo número (séries diferentes): não é duplicidade
        dup = None
        if len(nf_cands) == 1 and pn is not None:
            dup = ctx.dups.get(nf_cands[0]["chave_acesso"], {}).get(pn)
        paid = [t for t in live if t["status"] == "pago"]
        pending = [t for t in live if t["status"] in OPEN]
        if len(paid) >= 2:
            recovery.append((paid[0], {"outros": [p["titulo_id"] for p in paid[1:]], "dup": dup}))
        if not pending:
            continue
        if paid:
            keeper = paid[0]
            redundant = pending
        else:
            def rank(t):
                matches = dup is not None and abs(t["valor_cents"] - dup["valor_cents"]) <= ROUNDING_TOL
                return (not matches, t["origem_lancamento"] != "xml_portal", t["data_lancamento"], t["titulo_id"])
            ordered = sorted(pending, key=rank)
            keeper, redundant = ordered[0], ordered[1:]
        for t in redundant:
            certain = len(nf_cands) == 1 and dup is not None
            ev = _t_ev(t, "numero_nf_digitado", "parcela_n", "valor_cents", "status", "lote_remessa") + \
                 _t_ev(keeper, "numero_nf_digitado", "valor_cents", "status", "lote_remessa")
            if dup:
                ev.append(_ev("n_duplicatas", f"{nf_cands[0]['chave_acesso']}#{pn}", "valor_cents", dup["valor_cents"]))
            missing = [] if certain else ["NF única no portal para o número/parcela"]
            out.append(Finding(
                case_id=f"R1-{t['titulo_id']}", rule_id="R1_DUPLICIDADE", titulos=[t["titulo_id"], keeper["titulo_id"]],
                evidences=ev, severity=_sev(t["valor_cents"]), exposure_cents=t["valor_cents"],
                certainty=Certainty.comprovado if certain else Certainty.indicio,
                strength=Strength.forte if certain else Strength.moderada, fornecedor_id=t["fornecedor_id"],
                lote_remessa=t["lote_remessa"], missing_data=missing,
                summary=(f"Título {t['titulo_id']} repete NF {nf_num} parcela {pn}/{pt} já coberta por {keeper['titulo_id']} "
                         f"({keeper['status']}); digitação '{t['numero_nf_digitado']}' x '{keeper['numero_nf_digitado']}'."),
                recommended_action="retirar_do_lote" if t["status"] == "agendado" else "cancelar_titulo",
                required_permissions=["analista_cp"],
                required_approvals=["aprovador"] if t["valor_cents"] > 500_000 else [],
            ))
    if recovery:
        total = 0
        ev, titulos = [], []
        for t, info in recovery:
            total += t["valor_cents"]
            titulos += [t["titulo_id"]] + info["outros"]
            ev.append(_ev("op_titulos", t["titulo_id"], "pago_em_duplicidade_com", ",".join(info["outros"])))
        out.append(Finding(
            case_id="R1H-RECUPERACAO", rule_id="R1_DUPLICIDADE_HISTORICA", titulos=titulos, evidences=ev,
            severity=_sev(total), exposure_cents=total, certainty=Certainty.comprovado, strength=Strength.forte,
            summary=f"{len(recovery)} parcelas pagas duas vezes no histórico (perda já ocorrida; recuperação).",
            recommended_action="acionar_pessoa", notify=["PS09"], required_permissions=["auditor"]))
    return out


# ====================================================================== R2 — divergência de valor
def r2_valor(ctx: AuditContext) -> list[Finding]:
    out: list[Finding] = []
    disp_oc = {"utilidades"}
    for t in ctx.titulos.values():
        if t["status"] != "agendado":
            continue
        if not t["nf_num"]:
            continue  # sem NF é tratado em R6 (aprovação) e contrato abaixo
        nf, dup = ctx.duplicata_for(t)
        if nf is None or dup is None:
            continue  # NF ausente/ambígua é tratada em R5/R6
        pay = _pay_date(ctx, t)
        ceiling = _late_fee_max(dup["valor_cents"], dup["vencimento"], pay)
        diff = t["valor_cents"] - dup["valor_cents"]
        ev = _t_ev(t, "valor_cents", "numero_nf_digitado", "parcela_n") + [
            _ev("n_duplicatas", f"{nf['chave_acesso']}#{dup['n_dup']}", "valor_cents", dup["valor_cents"]),
            _ev("n_duplicatas", f"{nf['chave_acesso']}#{dup['n_dup']}", "vencimento", dup["vencimento"]),
            _ev("derivado", t["titulo_id"], "teto_legitimo_cents", ceiling, Provenance.derivado)]
        dda = next((d for d in ctx.dda_by_parcel.get((nf["cnpj_root"], t["nf_num"], str(t["parcela_n"])), [])), None)
        if dda:
            ev.append(_ev("n_dda", dda["dda_id"], "valor_cents", dda["valor_cents"]))
        if t["valor_cents"] > ceiling:
            excess = t["valor_cents"] - dup["valor_cents"]
            small = excess <= SMALL_DIFF_NO_CONTAIN
            out.append(Finding(
                case_id=f"R2-{t['titulo_id']}", rule_id="R2_VALOR_ACIMA_DUPLICATA", titulos=[t["titulo_id"]], evidences=ev,
                severity=_sev(excess), exposure_cents=excess, certainty=Certainty.comprovado,
                strength=Strength.forte, fornecedor_id=t["fornecedor_id"], lote_remessa=t["lote_remessa"],
                summary=(f"Título R$ {cents_to_str(t['valor_cents'])} acima da duplicata R$ {cents_to_str(dup['valor_cents'])} "
                         f"(excesso R$ {cents_to_str(excess)}; teto com acréscimos legítimos R$ {cents_to_str(ceiling)})."),
                recommended_action="corrigir_lancamento" if small or excess <= 500_000 else "retirar_do_lote+corrigir_lancamento",
                required_permissions=["analista_cp"],
                required_approvals=[] if excess <= 500_000 else (["aprovador"] if excess <= 5_000_000 else ["PS03"])))
        elif diff < -ROUNDING_TOL:
            out.append(Finding(
                case_id=f"R2-{t['titulo_id']}", rule_id="R2_VALOR_ABAIXO_DUPLICATA", titulos=[t["titulo_id"]], evidences=ev,
                severity=Severity.baixa, exposure_cents=0, certainty=Certainty.comprovado, strength=Strength.forte,
                fornecedor_id=t["fornecedor_id"], lote_remessa=t["lote_remessa"],
                summary=(f"Título R$ {cents_to_str(t['valor_cents'])} abaixo da duplicata R$ {cents_to_str(dup['valor_cents'])}: "
                         "pagamento parcial gera cobrança/multa; não há saída indevida."),
                recommended_action="corrigir_lancamento", required_permissions=["analista_cp"],
                required_approvals=[] if -diff <= 500_000 else ["aprovador"]))
        # NF x OC
        oc = ctx.ocs.get(t["oc_id"])
        if oc and t["categoria"] not in disp_oc and oc["cnpj_root"] == t["fornecedor_cnpj_root"]:
            base = oc["valor_mensal_cents"] if oc["tipo"] == "contrato" else oc["valor_total_cents"]
            if base and nf["valor_cents"] > base + max(int(base * 0.02), 5000):
                excess = nf["valor_cents"] - base
                comprador = "PS04" if t["categoria"] in ("produto", "embalagens") else "PS05"
                out.append(Finding(
                    case_id=f"R2OC-{t['titulo_id']}", rule_id="R2_NF_ACIMA_OC", titulos=[t["titulo_id"]],
                    evidences=[_ev("n_nf", nf["chave_acesso"], "valor_cents", nf["valor_cents"]),
                               _ev("n_oc", oc["oc_id"], "valor_base_cents", base)],
                    severity=_sev(excess), exposure_cents=excess, certainty=Certainty.indicio, strength=Strength.moderada,
                    fornecedor_id=t["fornecedor_id"], lote_remessa=t["lote_remessa"], notify=[comprador],
                    summary=f"NF acima da OC além da tolerância (2% ou R$ 50): excesso R$ {cents_to_str(excess)}.",
                    recommended_action="acionar_pessoa", required_permissions=["analista_cp"]))
    return out


# ====================================================================== R3 — pagamento já realizado
def r3_ja_pago(ctx: AuditContext) -> list[Finding]:
    out: list[Finding] = []
    manual = ctx.manual_payments_unexplained()
    for t in ctx.titulos.values():
        if t["status"] != "agendado":
            continue
        ev, sources = [], []
        dda = ctx.dda_by_linha.get(t["linha_digitavel"]) if t["linha_digitavel"] else None
        if dda and dda["situacao"] == "liquidado" and dda["data_situacao"] and dda["data_situacao"] <= ctx.sim_ts[:10]:
            ev.append(_ev("n_dda", dda["dda_id"], "situacao", f"liquidado em {dda['data_situacao']}"))
            sources.append("dda_liquidado")
        venc = date.fromisoformat(t["vencimento"])
        for m in manual:
            if t["fornecedor_id"] in m["fornecedores_candidatos"] and abs(-m["valor_cents"] - t["valor_cents"]) <= ROUNDING_TOL \
                    and -20 <= (date.fromisoformat(m["data"]) - venc).days <= 5 and m["data"] >= t["data_lancamento"][:10]:
                ev.append(_ev("n_extrato", m["mov_id"], "debito", f"{m['data']} {m['descricao']} {cents_to_str(-m['valor_cents'])}"))
                sources.append("extrato_pagamento_avulso")
                break
        if not sources:
            continue
        both = len(set(sources)) >= 2
        out.append(Finding(
            case_id=f"R3-{t['titulo_id']}", rule_id="R3_PAGO_ANTERIORMENTE", titulos=[t["titulo_id"]],
            evidences=_t_ev(t, "valor_cents", "status", "lote_remessa", "linha_digitavel") + ev,
            severity=_sev(t["valor_cents"]), exposure_cents=t["valor_cents"],
            certainty=Certainty.comprovado if both else Certainty.indicio,
            strength=Strength.forte, fornecedor_id=t["fornecedor_id"], lote_remessa=t["lote_remessa"],
            signals=sources, notify=["PS02"],
            missing_data=[] if both else ["segunda fonte independente (DDA ou extrato)"],
            summary=(f"Título agendado já consta como pago fora do lote ({', '.join(sorted(set(sources)))}); "
                     "baixa manual pendente no ERP."),
            recommended_action="retirar_do_lote", required_permissions=["analista_cp"]))
    return out


# ====================================================================== R4 — alteração bancária
def r4_alteracao_bancaria(ctx: AuditContext) -> list[Finding]:
    out: list[Finding] = []
    comms = ctx.comm_signals()
    for g in ctx.bank_change_groups():
        fid, ts, rows = g["fornecedor_id"], g["data_hora"], g["rows"]
        trusted_phone, phone_changed_at = ctx.phone_before(fid, ts)
        problems = []
        for r in rows:
            if r["confirmacao_registrada"] != "sim":
                problems.append(f"{r['alteracao_id']}: sem confirmação registrada")
                continue
            phones = ctx.phones_in(r["confirmacao_como"])
            if not phones:
                problems.append(f"{r['alteracao_id']}: confirmação sem ligação para telefone identificável")
            elif trusted_phone not in phones:
                problems.append(f"{r['alteracao_id']}: confirmação por telefone diferente do cadastro anterior ({trusted_phone})")
            elif phone_changed_at:
                problems.append(f"{r['alteracao_id']}: telefone trocado há menos de 30 dias ({phone_changed_at})")
        if not problems:
            continue
        affected = [t for t in ctx.titulos.values() if t["fornecedor_id"] == fid and t["status"] in OPEN
                    and t["forma_pagamento"] in ("ted", "pix")]
        f_comms = [c for c in comms.get(fid, []) if c["data_hora"] >= ts[:10] or abs((date.fromisoformat(c["data_hora"][:10]) - date.fromisoformat(ts[:10])).days) <= 3]
        signals = sorted({s for c in f_comms for s in c["signals"] if s not in ("remetente_cadastrado", "dominio_cadastrado", "fornecedor_citado")})
        ev = [_ev("raw_alteracoes_cadastro", r["alteracao_id"], r["campo"], f"{r['valor_anterior']} -> {r['valor_novo']} "
                  f"(origem {r['origem']}, confirmação '{r['confirmacao_registrada']}')") for r in rows]
        ev += [_ev("raw_comunicacoes_fornecedores", c["comunicacao_id"], "sinais", ",".join(c["signals"])) for c in f_comms]
        ev += [_ev("op_titulos", t["titulo_id"], "destino", f"{t['forma_pagamento']} {t['lote_remessa'] or 'sem lote'}") for t in affected]
        exposure = sum(t["valor_cents"] for t in affected)
        fraud = any(s.startswith("dominio_semelhante") or s.startswith("injecao") for s in signals)
        out.append(Finding(
            case_id=f"R4-{fid}-{rows[0]['alteracao_id']}", rule_id="R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA",
            titulos=[t["titulo_id"] for t in affected], evidences=ev,
            severity=_sev(exposure, Severity.alta), exposure_cents=exposure,
            certainty=Certainty.comprovado,   # violação da política de troca de conta está demonstrada
            strength=Strength.forte, fornecedor_id=fid, signals=signals + problems,
            notify=["PS01", "PS08"] if fraud else ["PS01"],
            missing_data=["confirmação por ligação ao telefone anterior à alteração"],
            summary=(f"Dados bancários de {fid} alterados em {ts} sem confirmação válida; {len(affected)} título(s) "
                     f"TED/PIX iriam para a conta nova (R$ {cents_to_str(exposure)})."
                     + (" Comunicação de domínio semelhante ao cadastrado e/ou instruções a automações detectadas." if fraud else "")),
            recommended_action="bloquear_titulo+reverter_alteracao_cadastro",
            required_permissions=["auditor"], required_approvals=["aprovador", "PS01"]))
    return out


# ====================================================================== R5 — beneficiário incompatível
def r5_beneficiario(ctx: AuditContext) -> list[Finding]:
    out: list[Finding] = []
    comms = ctx.comm_signals()
    for t in ctx.titulos.values():
        if t["status"] != "agendado":
            continue
        root = t["fornecedor_cnpj_root"]
        # (a) boleto com beneficiário de outra raiz de CNPJ
        if t["forma_pagamento"] == "boleto" and t["cnpj_beneficiario_root"] and t["cnpj_beneficiario_root"] != root:
            legit = [d for d in ctx.dda_by_parcel.get((root, t["nf_num"], str(t["parcela_n"])), [])]
            hist = sorted(ctx.boleto_banks_by_root.get(root, set()))
            f_comms = [c for c in comms.get(t["fornecedor_id"], []) if t["nf_num"] and t["nf_num"] in c["assunto"]]
            ev = _t_ev(t, "cnpj_beneficiario", "linha_digitavel", "valor_cents") + [
                _ev("n_fornecedores", t["fornecedor_id"], "cnpj_root", root),
                _ev("derivado", t["titulo_id"], "bancos_cobranca_historicos", ",".join(hist), Provenance.derivado)]
            ev += [_ev("n_dda", d["dda_id"], "boleto_registrado", f"banco {d['banco_cobrador']} CNPJ {d['cnpj_beneficiario']} "
                       f"valor {cents_to_str(d['valor_cents'])} {d['situacao']}") for d in legit]
            ev += [_ev("raw_comunicacoes_fornecedores", c["comunicacao_id"], "sinais", ",".join(c["signals"])) for c in f_comms]
            signals = sorted({s for c in f_comms for s in c["signals"]})
            out.append(Finding(
                case_id=f"R5-{t['titulo_id']}", rule_id="R5_BENEFICIARIO_INCOMPATIVEL", titulos=[t["titulo_id"]], evidences=ev,
                severity=_sev(t["valor_cents"], Severity.alta), exposure_cents=t["valor_cents"], certainty=Certainty.comprovado,
                strength=Strength.forte, fornecedor_id=t["fornecedor_id"], lote_remessa=t["lote_remessa"], signals=signals,
                notify=["PS01", "PS08"] if any("dominio" in s or "email_gratuito" in s or "injecao" in s for s in signals) else ["PS01"],
                missing_data=[] if legit else ["boleto registrado no DDA para a mesma NF/parcela"],
                summary=(f"Boleto paga CNPJ {t['cnpj_beneficiario']} (banco {t['ld_banco']}), raiz diferente do fornecedor ({root}); "
                         + (f"DDA registra o boleto legítimo no banco {legit[0]['banco_cobrador']}." if legit else "sem boleto no DDA.")),
                recommended_action="bloquear_titulo+contatar_fornecedor", required_permissions=["auditor"]))
            continue
        # (b) fornecedor escolhido errado: NF/OC pertencem a outra empresa
        if t["nf_num"] and not ctx.nfs.get((root, t["nf_num"])):
            oc = ctx.ocs.get(t["oc_id"])
            if oc and oc["cnpj_root"] != root:
                nf_other = [n for n in ctx.nfs.get((oc["cnpj_root"], t["nf_num"]), [])]
                other = ctx.forn_by_root.get(oc["cnpj_root"], [{}])[0]
                ev = _t_ev(t, "fornecedor_id", "numero_nf_digitado", "oc_id", "valor_cents") + [
                    _ev("n_oc", oc["oc_id"], "cnpj", oc["cnpj"]), _ev("n_fornecedores", t["fornecedor_id"], "cnpj_root", root)]
                ev += [_ev("n_nf", n["chave_acesso"], "emitente", f"{n['cnpj_emitente']} {n['razao_social_emitente']}") for n in nf_other]
                certain = bool(nf_other)
                out.append(Finding(
                    case_id=f"R5F-{t['titulo_id']}", rule_id="R5_FORNECEDOR_DIVERGENTE", titulos=[t["titulo_id"]], evidences=ev,
                    severity=_sev(t["valor_cents"], Severity.media), exposure_cents=t["valor_cents"],
                    certainty=Certainty.comprovado if certain else Certainty.indicio,
                    strength=Strength.forte if certain else Strength.moderada, fornecedor_id=t["fornecedor_id"],
                    lote_remessa=t["lote_remessa"], missing_data=[] if certain else ["NF da outra empresa no portal"],
                    summary=(f"Título lançado para {t['fornecedor_id']}, mas OC {oc['oc_id']} e NF {t['nf_num']} são de "
                             f"{other.get('fornecedor_id', '?')} ({oc['nome_fornecedor']}): pagamento iria para terceiro."),
                    recommended_action="retirar_do_lote+corrigir_lancamento(fornecedor_id)",
                    required_permissions=["analista_cp"], required_approvals=["aprovador"]))
    return out


# ====================================================================== R6 — aprovação inadequada ou ausente
def r6_aprovacao(ctx: AuditContext) -> list[Finding]:
    out: list[Finding] = []
    for t in ctx.titulos.values():
        if t["status"] != "agendado":
            continue
        reasons, ev = [], _t_ev(t, "oc_id", "numero_nf_digitado", "valor_cents", "nivel_aprovacao")
        oc = ctx.ocs.get(t["oc_id"])
        if t["categoria"] != "utilidades":
            if not t["oc_id"]:
                reasons.append("sem OC")
            elif oc and oc["status"] not in ("aprovada", "encerrada"):
                reasons.append(f"OC {oc['oc_id']} com status '{oc['status']}'")
                ev.append(_ev("n_oc", oc["oc_id"], "status", oc["status"]))
        if not t["nf_num"] and t["categoria"] != "aluguel":
            reasons.append("sem NF")
        if not reasons:
            continue
        comprador = "PS04" if t["categoria"] in ("produto", "embalagens") else "PS05"
        out.append(Finding(
            case_id=f"R6-{t['titulo_id']}", rule_id="R6_APROVACAO_DUPLA_AUSENTE", titulos=[t["titulo_id"]], evidences=ev,
            severity=_sev(t["valor_cents"], Severity.media), exposure_cents=t["valor_cents"], certainty=Certainty.comprovado,
            strength=Strength.moderada, fornecedor_id=t["fornecedor_id"], lote_remessa=t["lote_remessa"],
            notify=[comprador] if oc and oc["status"] == "cancelada" else [],
            summary=f"Pagamento {', '.join(reasons)} exige aprovação dupla (política) e não há aprovação registrada.",
            recommended_action="retirar_do_lote+solicitar_aprovacao(agendar_titulo)",
            required_permissions=["analista_cp"], required_approvals=["aprovador", "PS01"]))
    # fracionamento: mesmo fornecedor + mesma NF + mesmo dia, cada um na alçada do analista, soma acima
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in ctx.titulos.values():
        if t["status"] == "agendado" and t["nf_num"]:
            groups[(t["fornecedor_id"], t["nf_num"], _pay_date(ctx, t))].append(t)
    limit = 500_000
    for (fid, nf_num, day), g in groups.items():
        if len({(t["parcela_n"], t["parcela_total"]) for t in g}) < len(g):
            continue  # mesma parcela repetida é duplicidade (R1), não fracionamento
        if len(g) > 1 and all(t["valor_cents"] <= limit for t in g) and sum(t["valor_cents"] for t in g) > limit:
            total = sum(t["valor_cents"] for t in g)
            out.append(Finding(
                case_id=f"R6F-{fid}-{nf_num}-{day}", rule_id="R6_FRACIONAMENTO", titulos=[t["titulo_id"] for t in g],
                evidences=[_ev("op_titulos", t["titulo_id"], "valor_cents", t["valor_cents"]) for t in g],
                severity=Severity.media, exposure_cents=0, certainty=Certainty.indicio, strength=Strength.moderada,
                fornecedor_id=fid, summary=f"{len(g)} títulos da NF {nf_num} no mesmo dia somam R$ {cents_to_str(total)} (> alçada do analista).",
                recommended_action="exigir aprovador pela soma", required_permissions=["analista_cp"], required_approvals=["aprovador"]))
    # histórico: pagos acima da alçada sem aprovação adequada
    hist = []
    for t in ctx.titulos.values():
        if t["status"] != "pago" or t["nivel_aprovacao"] == "analista":
            continue
        aps = [a for a in ctx.aprovacoes.get(t["titulo_id"], []) if a["decisao"] == "aprovado"]
        bad = None
        if not aps:
            bad = "sem aprovação registrada"
        elif any(a["aprovador"] == t["lancado_por"] for a in aps):
            bad = "aprovador = lançador"
        elif t["nivel_aprovacao"] == "diretoria" and not any(ctx.usuarios.get(a["aprovador"], {}).get("perfil") == "diretoria" for a in aps):
            bad = "nível diretoria aprovado sem diretoria"
        if bad:
            hist.append((t, bad))
    if hist:
        out.append(Finding(
            case_id="R6H-HISTORICO", rule_id="R6_APROVACAO_HISTORICA", titulos=[t["titulo_id"] for t, _ in hist],
            evidences=[_ev("op_titulos", t["titulo_id"], "aprovacao", b) for t, b in hist],
            severity=Severity.media, exposure_cents=0, certainty=Certainty.comprovado, strength=Strength.forte,
            summary=f"{len(hist)} pagamentos históricos com aprovação inadequada.", recommended_action="propor_correcao_processo",
            required_permissions=["auditor"]))
    return out


# ====================================================================== R7 — comunicações suspeitas (sem título)
def r7_comunicacoes(ctx: AuditContext) -> list[Finding]:
    out = []
    seen = set()
    for fid, items in ctx.comm_signals().items():
        for c in items:
            if c["comunicacao_id"] in seen:
                continue
            s = c["signals"]
            phishing = "anexo_compactado_de_desconhecido" in s
            injection = any(x in ("injecao:instrucao_ao_sistema", "injecao:alegacao_de_aprovacao", "injecao:instrucao_a_automacao")
                            for x in s)
            unofficial = "email_gratuito_nao_cadastrado" in s and "fornecedor_citado_por_terceiro" in s
            lookalike = "dominio_semelhante_ao_cadastrado" in s
            if not (phishing or injection or lookalike or unofficial):
                continue
            seen.add(c["comunicacao_id"])
            kind = ("phishing" if phishing else "dominio_semelhante" if lookalike else
                    "remetente_nao_oficial" if unofficial else "instrucao_a_automacao")
            out.append(Finding(
                case_id=f"R7-{c['comunicacao_id']}", rule_id="R7_COMUNICACAO_SUSPEITA", titulos=[],
                evidences=[_ev("raw_comunicacoes_fornecedores", c["comunicacao_id"], "sinais", ",".join(s))],
                severity=Severity.media, exposure_cents=0, certainty=Certainty.indicio, strength=Strength.forte,
                fornecedor_id="" if fid == "_sem_fornecedor" else fid, signals=s,
                summary=f"Comunicação {c['comunicacao_id']} de {c['remetente']} classificada como {kind}; tratada só como dado.",
                recommended_action="abrir_incidente", required_permissions=["auditor"]))
    return out


ALL_RULES = (r1_duplicidade, r2_valor, r3_ja_pago, r4_alteracao_bancaria, r5_beneficiario, r6_aprovacao, r7_comunicacoes)


def run_rules(ctx: AuditContext) -> list[Finding]:
    findings: list[Finding] = []
    for rule in ALL_RULES:
        findings.extend(rule(ctx))
    return subsume(findings)


# prioridade por título: contenção por fraude/pagamento indevido prevalece sobre correção de valor/aprovação
RULE_PRIORITY = {"R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA": 0, "R5_BENEFICIARIO_INCOMPATIVEL": 1, "R3_PAGO_ANTERIORMENTE": 2,
                 "R1_DUPLICIDADE": 3, "R5_FORNECEDOR_DIVERGENTE": 4, "R2_VALOR_ACIMA_DUPLICATA": 5,
                 "R6_APROVACAO_DUPLA_AUSENTE": 6, "R2_NF_ACIMA_OC": 7, "R2_VALOR_ABAIXO_DUPLICATA": 8}


def subsume(findings: list[Finding]) -> list[Finding]:
    """Um título recebe UMA ação principal. Achados de menor prioridade sobre o mesmo título viram sinal do
    achado principal (rastreável), evitando ações conflitantes (ex.: corrigir valor de um título duplicado)."""
    owner: dict[str, Finding] = {}
    for f in sorted(findings, key=lambda f: RULE_PRIORITY.get(f.rule_id, 99)):
        if f.rule_id not in RULE_PRIORITY:
            continue
        primary = f.titulos[0] if f.rule_id != "R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA" else None
        for tid in ([primary] if primary else f.titulos):
            owner.setdefault(tid, f)
    out, extra = [], defaultdict(list)
    for f in findings:
        if f.rule_id in RULE_PRIORITY and f.rule_id != "R4_ALTERACAO_BANCARIA_NAO_CONFIRMADA":
            o = owner.get(f.titulos[0])
            if o is not None and o.case_id != f.case_id:
                extra[o.case_id].append(f"subsumido:{f.rule_id}")
                continue
        out.append(f)
    return [f.model_copy(update={"signals": f.signals + extra[f.case_id]}) if extra.get(f.case_id) else f for f in out]
