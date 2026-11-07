"""Contexto de auditoria: índices em memória sobre n_* (derivado) + op_* (estado atual).

Tudo aqui é determinístico e reprodutível a partir do banco. Nenhum valor é inventado: quando um
dado não existe, a regra registra o campo em `missing_data`.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from ..guard import injection_signals
from ..normalize import decode_linha_digitavel, normalize_name

FREE_MAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.com.br", "bol.com.br", "uol.com.br"}
BANK_FIELDS = ("banco", "agencia", "conta", "chave_pix")
_PHONE_RX = re.compile(r"\(\d{2}\)\s*\d{4,5}-\d{4}")


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class AuditContext:
    def __init__(self, conn: sqlite3.Connection, sim_ts: str):
        self.conn, self.sim_ts = conn, sim_ts
        self.today = _d(sim_ts)
        q = lambda sql, a=(): [dict(r) for r in conn.execute(sql, a)]  # noqa: E731

        self.fornecedores = {f["fornecedor_id"]: f for f in q(
            "SELECT n.*, o.banco AS op_banco, o.agencia AS op_agencia, o.conta AS op_conta, o.chave_pix AS op_chave_pix,"
            " o.telefone_cadastrado AS op_telefone, o.bloqueado AS forn_bloqueado FROM n_fornecedores n JOIN op_fornecedores o USING(fornecedor_id)")}
        self.forn_by_root: dict[str, list[dict]] = defaultdict(list)
        for f in self.fornecedores.values():
            self.forn_by_root[f["cnpj_root"]].append(f)

        # títulos: estado ATUAL (op_*) + atributos derivados (n_*); linha digitável redecodificada do estado atual
        self.titulos: dict[str, dict] = {}
        for t in q("SELECT n.titulo_id, n.nf_num, n.nf_sufixo, n.parcela_n, n.parcela_total, n.categoria, n.cnpj_beneficiario,"
                   " n.cnpj_beneficiario_root, n.origem_lancamento, n.lancado_por, n.data_lancamento, n.nivel_aprovacao,"
                   " n.aprovado_por, n.historico, o.fornecedor_id, o.numero_nf_digitado, o.oc_id, o.valor_cents, o.vencimento,"
                   " o.forma_pagamento, o.linha_digitavel, o.status, o.lote_remessa, o.data_pagamento, o.bloqueado"
                   " FROM op_titulos o JOIN n_titulos n USING(titulo_id)"):
            f = self.fornecedores.get(t["fornecedor_id"], {})
            t["fornecedor_cnpj_root"] = f.get("cnpj_root", "")
            t["categoria"] = f.get("categoria", t["categoria"])
            ld = decode_linha_digitavel(t["linha_digitavel"]) or {}
            t["ld_banco"], t["ld_valor_cents"] = ld.get("banco", ""), ld.get("valor_cents")
            self.titulos[t["titulo_id"]] = t
        self.by_parcel: dict[tuple, list[dict]] = defaultdict(list)
        for t in self.titulos.values():
            if t["nf_num"]:
                self.by_parcel[(t["fornecedor_cnpj_root"], t["nf_num"], t["parcela_n"], t["parcela_total"])].append(t)

        self.lotes = {r["lote_remessa"]: r for r in q("SELECT * FROM op_lotes")}
        self.nfs: dict[tuple, list[dict]] = defaultdict(list)
        self.nfs_by_num: dict[str, list[dict]] = defaultdict(list)
        for n in q("SELECT * FROM n_nf"):
            self.nfs[(n["cnpj_root"], n["nf_num"])].append(n)
            self.nfs_by_num[n["nf_num"]].append(n)
        self.dups: dict[str, dict[int, dict]] = defaultdict(dict)
        for d in q("SELECT * FROM n_duplicatas"):
            self.dups[d["chave_acesso"]][d["n_dup"]] = d
        self.ocs = {o["oc_id"]: o for o in q("SELECT * FROM n_oc")}
        self.dda_by_linha = {d["linha_digitavel"]: d for d in q("SELECT * FROM n_dda")}
        self.dda_by_parcel: dict[tuple, list[dict]] = defaultdict(list)
        self.boleto_banks_by_root: dict[str, set[str]] = defaultdict(set)
        for d in self.dda_by_linha.values():
            self.dda_by_parcel[(d["cnpj_beneficiario_root"], d["doc_nf_num"], d["doc_parcela"])].append(d)
            self.boleto_banks_by_root[d["cnpj_beneficiario_root"]].add(d["banco_cobrador"])
        for t in self.titulos.values():
            if t["status"] == "pago" and t["ld_banco"] and t["cnpj_beneficiario_root"] == t["fornecedor_cnpj_root"]:
                self.boleto_banks_by_root[t["fornecedor_cnpj_root"]].add(t["ld_banco"])
        self.alteracoes: dict[str, list[dict]] = defaultdict(list)
        for a in q("SELECT * FROM raw_alteracoes_cadastro ORDER BY data_hora"):
            self.alteracoes[a["fornecedor_id"]].append(a)
        self.contratos = {c["contrato_id"]: c for c in q("SELECT * FROM raw_contratos_fornecedores")}
        self.usuarios = {u["login"]: u for u in q("SELECT * FROM raw_usuarios_sistema")}
        self.aprovacoes: dict[str, list[dict]] = defaultdict(list)
        for a in q("SELECT * FROM raw_aprovacoes"):
            self.aprovacoes[a["titulo_id"]].append(a)
        self.comms = [c for c in q("SELECT * FROM raw_comunicacoes_fornecedores ORDER BY data_hora")
                      if c["data_hora"] <= sim_ts.replace("T", " ")]
        self._manual_payments = None
        self._comm_signals = None

    # ------------------------------------------------------------------ helpers
    def lote_open(self, lote: str) -> bool:
        l = self.lotes.get(lote)
        if not l or l["status"] != "aberto":
            return False
        close = datetime.fromisoformat(f"{l['data_prevista']}T{l['horario_fechamento']}:00")
        return datetime.fromisoformat(self.sim_ts) < close

    def lote_close_ts(self, lote: str) -> str:
        l = self.lotes.get(lote)
        return f"{l['data_prevista']}T{l['horario_fechamento']}:00" if l else ""

    def nf_for(self, t: dict) -> tuple[dict | None, list[dict]]:
        """NF do título pela raiz do CNPJ do fornecedor + número normalizado (série desempata)."""
        cands = self.nfs.get((t["fornecedor_cnpj_root"], t["nf_num"]), [])
        if len(cands) > 1 and t["nf_sufixo"]:
            by_serie = [n for n in cands if n["serie"] == t["nf_sufixo"]]
            if len(by_serie) == 1:
                return by_serie[0], cands
        if len(cands) > 1 and t["oc_id"]:
            by_oc = [n for n in cands if n["oc_informada"] == t["oc_id"]]
            if len(by_oc) == 1:
                return by_oc[0], cands
        return (cands[0] if len(cands) == 1 else None), cands

    def duplicata_for(self, t: dict) -> tuple[dict | None, dict | None]:
        nf, _ = self.nf_for(t)
        if nf is None or t["parcela_n"] is None:
            return nf, None
        return nf, self.dups.get(nf["chave_acesso"], {}).get(t["parcela_n"])

    # ------------------------------------------------------------------ extratos: pagamentos fora do lote não explicados
    def manual_payments_unexplained(self) -> list[dict]:
        """Débitos do extrato sem titulo_id que NÃO casam com título pago na mesma data/valor/fornecedor."""
        if self._manual_payments is not None:
            return self._manual_payments
        names = [(f["fornecedor_id"], f["nome_norm"], normalize_name(f["nome_fantasia"])) for f in self.fornecedores.values()]

        def cands(desc: str) -> list[str]:
            d = re.sub(r"^(TED AVULSA|BOLETO AVULSA|PIX AVULSA|PIX ENV \d+|TED ENV \d{3}|PAG BOL \d{3}|BOLETO \d{3}|TED \d{3})\s+", "", desc)
            d = normalize_name(d)
            toks = d.split()
            if not toks:
                return []
            trunc = " ".join(toks[:-1]) if len(toks) >= 2 else d
            return [fid for fid, n1, n2 in names if n1.startswith(d) or n2.startswith(d) or (len(toks) >= 2 and (n1.startswith(trunc) or n2.startswith(trunc)))]

        paid_index: dict[tuple, list[str]] = defaultdict(list)
        for t in self.titulos.values():
            if t["status"] == "pago" and t["data_pagamento"]:
                paid_index[(t["fornecedor_id"], t["valor_cents"], t["data_pagamento"])].append(t["titulo_id"])
        used: set[str] = set()
        out = []
        for m in self.conn.execute("SELECT * FROM n_extrato WHERE tipo LIKE 'PAGTO%' AND titulo_id='' AND data<=? ORDER BY data, mov_id",
                                   (self.sim_ts[:10],)):
            m = dict(m)
            fids = cands(m["descricao"])
            hit = None
            for fid in fids:
                for tid in paid_index.get((fid, -m["valor_cents"], m["data"]), []):
                    if tid not in used:
                        hit = tid
                        break
                if hit:
                    break
            if hit:
                used.add(hit)
                continue
            m["fornecedores_candidatos"] = fids
            out.append(m)
        self._manual_payments = out
        return out

    # ------------------------------------------------------------------ comunicações: sinais determinísticos
    def comm_signals(self) -> dict[str, list[dict]]:
        """Sinais por comunicação: remetente oficial / mesmo domínio / domínio semelhante / e-mail gratuito /
        injeção. Vincula ao fornecedor por endereço, domínio, domínio semelhante ou nome citado."""
        if self._comm_signals is not None:
            return self._comm_signals
        reg_addr = {f["email_cadastrado"].lower(): fid for fid, f in self.fornecedores.items()}
        reg_dom: dict[str, list[str]] = defaultdict(list)
        for fid, f in self.fornecedores.items():
            dom = f["email_cadastrado"].split("@")[-1].lower()
            if dom not in FREE_MAIL:
                reg_dom[dom].append(fid)
        name_keys = {fid: normalize_name(f["nome_fantasia"]) for fid, f in self.fornecedores.items()
                     if len(normalize_name(f["nome_fantasia"])) >= 6}
        out: dict[str, list[dict]] = {}
        for c in self.comms:
            sender = c["remetente"].lower()
            dom = sender.split("@")[-1]
            sig, linked = [], set()
            if sender in reg_addr:
                linked.add(reg_addr[sender]); sig.append("remetente_cadastrado")
            elif dom in reg_dom:
                linked.update(reg_dom[dom]); sig.append("dominio_cadastrado")
            else:
                for d2, fids in reg_dom.items():
                    if 0 < levenshtein(dom, d2) <= 2:
                        linked.update(fids); sig.append("dominio_semelhante_ao_cadastrado")
                if dom in FREE_MAIL:
                    sig.append("email_gratuito_nao_cadastrado")
                if not linked:
                    sig.append("remetente_desconhecido")
            text = f"{c['assunto']} {c['texto']}"
            ntext = normalize_name(text)
            for fid, key in name_keys.items():
                if key and key in ntext:
                    if fid not in linked:
                        sig.append("fornecedor_citado_por_terceiro" if "remetente_cadastrado" not in sig and "dominio_cadastrado" not in sig else "fornecedor_citado")
                    linked.add(fid)
            sig += [f"injecao:{s}" for s in injection_signals(text)]
            if re.search(r"\.zip\b", c["anexos"] or "", re.I) and "remetente_desconhecido" in sig:
                sig.append("anexo_compactado_de_desconhecido")
            rec = {"comunicacao_id": c["comunicacao_id"], "data_hora": c["data_hora"], "remetente": c["remetente"],
                   "assunto": c["assunto"], "signals": sorted(set(sig)), "fornecedores": sorted(linked)}
            for fid in linked or {"_sem_fornecedor"}:
                out.setdefault(fid, []).append(rec)
        self._comm_signals = out
        return out

    # ------------------------------------------------------------------ cadastro bancário
    def bank_change_groups(self, lookback_days: int = 120) -> list[dict]:
        """Agrupa alterações de dados bancários por (fornecedor, data_hora) dentro da janela."""
        groups = []
        for fid, alts in self.alteracoes.items():
            by_ts: dict[str, list[dict]] = defaultdict(list)
            for a in alts:
                if a["campo"] in BANK_FIELDS and a["data_hora"] <= self.sim_ts.replace("T", " "):
                    by_ts[a["data_hora"]].append(a)
            for ts, rows in by_ts.items():
                if (self.today - _d(ts)).days <= lookback_days:
                    groups.append({"fornecedor_id": fid, "data_hora": ts, "rows": rows})
        return groups

    def phone_before(self, fid: str, ts: str) -> tuple[str, str | None]:
        """Telefone que estava no cadastro antes de alterações recentes (<30 dias) anteriores a `ts`.
        Retorna (telefone_confiavel, data_da_troca_recente_ou_None)."""
        phones = [a for a in self.alteracoes.get(fid, []) if a["campo"] == "telefone"]
        current = self.fornecedores[fid]["telefone_cadastrado"]
        # reconstrói o telefone vigente em `ts`
        after = [a for a in phones if a["data_hora"] > ts]
        phone_at_ts = after[0]["valor_anterior"] if after else current
        recent = [a for a in phones if a["data_hora"] <= ts and (_d(ts) - _d(a["data_hora"])).days < 30]
        if recent:
            return recent[0]["valor_anterior"], recent[0]["data_hora"]
        return phone_at_ts, None

    @staticmethod
    def phones_in(text: str) -> list[str]:
        return [re.sub(r"\s+", " ", p) for p in _PHONE_RX.findall(text or "")]
