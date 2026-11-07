"""Ingestão reprodutível: CSV originais -> raw_* (fiel) -> n_* (derivado) -> op_* (estado de trabalho).

Os CSVs nunca são modificados: são lidos em modo somente leitura e o SHA-256 de cada um é gravado
em ingest_manifest. Reexecutar a ingestão sobre um banco novo produz o mesmo resultado.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from . import events
from .clock import BankCalendar
from .log import get_logger, log
from .normalize import (
    cnpj_digits, cnpj_root, cnpj_valid, decode_chave_acesso, decode_linha_digitavel, normalize_name,
    normalize_nf_number, parse_date, parse_destino, parse_litoral_historico, to_cents,
)
from .store import set_state, transaction

LOG = get_logger("ingest")

EXPECTED_FILES = [
    "agenda_remessa.csv", "alteracoes_cadastro.csv", "aprovacoes.csv", "centros_custo.csv",
    "comunicacoes_fornecedores.csv", "contas_a_pagar.csv", "contas_bancarias_empresa.csv",
    "contratos_fornecedores.csv", "dda_boletos.csv", "extrato_banco_litoral.csv",
    "extrato_banco_meridiano.csv", "feriados_bancarios.csv", "ferramentas.csv", "fornecedores.csv",
    "log_auditoria_erp.csv", "lotes_remessa.csv", "nf_duplicatas.csv", "notas_fiscais.csv",
    "pedidos_compra.csv", "plano_contas.csv", "politicas.csv", "recebimentos_mercadoria.csv",
    "retorno_remessa.csv", "usuarios_sistema.csv",
]
SEPARATORS = {"extrato_banco_litoral.csv": ";"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=SEPARATORS.get(path.name, ","))
        rows = list(r)
        return list(r.fieldnames or []), rows


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _create_table(conn: sqlite3.Connection, name: str, rows: list[dict], pk: str | None = None,
                  columns: list[str] | None = None) -> None:
    cols = columns or (list(rows[0].keys()) if rows else [])
    conn.execute(f"DROP TABLE IF EXISTS {_q(name)}")
    defs = []
    for c in cols:
        sample = next((r[c] for r in rows if r.get(c) is not None and r.get(c) != ""), None)
        typ = "INTEGER" if isinstance(sample, (int, bool)) else "TEXT"
        defs.append(f"{_q(c)} {typ}" + (" PRIMARY KEY" if c == pk else ""))
    conn.execute(f"CREATE TABLE {_q(name)} ({', '.join(defs)})")
    if rows:
        ph = ",".join("?" * len(cols))
        conn.executemany(f"INSERT INTO {_q(name)} VALUES ({ph})",
                         [tuple(r.get(c) for c in cols) for r in rows])


# ------------------------------------------------------------------ camada n_* (derivada)
def _n_fornecedores(raw):
    return [dict(fornecedor_id=r["fornecedor_id"], razao_social=r["razao_social"], nome_fantasia=r["nome_fantasia"],
                 nome_norm=normalize_name(r["razao_social"]), cnpj=cnpj_digits(r["cnpj"]), cnpj_root=cnpj_root(r["cnpj"]),
                 cnpj_valido=int(cnpj_valid(r["cnpj"])), categoria=r["categoria"], banco=r["banco"], agencia=r["agencia"],
                 conta=r["conta"], chave_pix=r["chave_pix"], forma_pagamento_padrao=r["forma_pagamento_padrao"],
                 email_cadastrado=r["email_cadastrado"], telefone_cadastrado=r["telefone_cadastrado"],
                 contrato_id=r["contrato_id"], responsavel_interno=r["responsavel_interno"],
                 data_ultima_alteracao=r["data_ultima_alteracao"]) for r in raw]


def _n_titulos(raw, forn_by_id):
    out = []
    for r in raw:
        nf_num, nf_suf = normalize_nf_number(r["numero_nf_digitado"])
        p = r["parcela"].split("/") if "/" in r["parcela"] else ["", ""]
        ld = decode_linha_digitavel(r["linha_digitavel"]) or {}
        f = forn_by_id.get(r["fornecedor_id"], {})
        out.append(dict(
            titulo_id=r["titulo_id"], fornecedor_id=r["fornecedor_id"], categoria=f.get("categoria", ""),
            fornecedor_cnpj_root=f.get("cnpj_root", ""), numero_nf_digitado=r["numero_nf_digitado"],
            nf_num=nf_num, nf_sufixo=nf_suf or "", oc_id=r["oc_id"],
            parcela_n=int(p[0]) if p[0].isdigit() else None, parcela_total=int(p[1]) if p[1].isdigit() else None,
            valor_cents=to_cents(r["valor"]), vencimento=r["vencimento"], forma_pagamento=r["forma_pagamento"],
            linha_digitavel=r["linha_digitavel"], ld_banco=ld.get("banco", ""), ld_valor_cents=ld.get("valor_cents"),
            ld_vencimento=ld.get("vencimento", ""), ld_nosso_numero=ld.get("nosso_numero", ""),
            ld_dv_ok=(int(ld["dv_campos_ok"] and ld["dv_geral_ok"]) if ld.get("valida_formato") else None),
            cnpj_beneficiario=cnpj_digits(r["cnpj_beneficiario_boleto"]),
            cnpj_beneficiario_root=cnpj_root(r["cnpj_beneficiario_boleto"]),
            origem_lancamento=r["origem_lancamento"], lancado_por=r["lancado_por"], data_lancamento=r["data_lancamento"],
            status=r["status"], data_pagamento=r["data_pagamento"], banco_pagador=r["banco_pagador"],
            lote_remessa=r["lote_remessa"], centro_custo=r["centro_custo"], nivel_aprovacao=r["nivel_aprovacao"],
            aprovado_por=r["aprovado_por"], baixado_por=r["baixado_por"], historico=r["historico"]))
    return out


def _n_nf(raw):
    out = []
    for r in raw:
        k = decode_chave_acesso(r["chave_acesso"])
        out.append(dict(chave_acesso=r["chave_acesso"], modelo=r["modelo"], numero_nf=r["numero_nf"],
                        nf_num=r["numero_nf"].lstrip("0") or "0", serie=r["serie"], cnpj_emitente=r["cnpj_emitente"],
                        cnpj_root=cnpj_root(r["cnpj_emitente"]), razao_social_emitente=r["razao_social_emitente"],
                        data_emissao=r["data_emissao"], valor_cents=to_cents(r["valor_total"]),
                        oc_informada=r["oc_informada"], data_recebimento_portal=r["data_recebimento_portal"],
                        chave_dv_ok=int(bool(k.get("dv_ok"))), situacao_sefaz=r["situacao_sefaz"],
                        informacoes_complementares=r["informacoes_complementares"]))
    return out


def _n_oc(raw):
    return [dict(oc_id=r["oc_id"], cod_fornecedor_compras=r["cod_fornecedor_compras"], cnpj=r["cnpj_fornecedor"],
                 cnpj_root=cnpj_root(r["cnpj_fornecedor"]), nome_fornecedor=r["nome_fornecedor"], tipo=r["tipo"],
                 valor_total_cents=to_cents(r["valor_total"]), valor_mensal_cents=to_cents(r["valor_mensal"]),
                 condicao_pagamento=r["condicao_pagamento"], status=r["status"], aprovado_por=r["aprovado_por"],
                 data_emissao=r["data_emissao"], centro_custo=r["centro_custo"]) for r in raw]


def _n_dda(raw):
    out = []
    for r in raw:
        num, par = (r["numero_documento"].split("/") + [""])[:2]
        out.append(dict(dda_id=r["dda_id"], data_registro=r["data_registro"], cnpj_pagador=r["cnpj_pagador"],
                        banco_cobrador=r["banco_cobrador"], beneficiario=r["beneficiario"],
                        cnpj_beneficiario=cnpj_digits(r["cnpj_beneficiario"]),
                        cnpj_beneficiario_root=cnpj_root(r["cnpj_beneficiario"]), numero_documento=r["numero_documento"],
                        doc_nf_num=num.lstrip("0"), doc_parcela=par, nosso_numero=r["nosso_numero"],
                        valor_cents=to_cents(r["valor"]), vencimento=r["vencimento"], linha_digitavel=r["linha_digitavel"],
                        situacao=r["situacao"], data_situacao=r["data_situacao"]))
    return out


def _n_extrato(mer, lit):
    out = []
    for i, r in enumerate(mer):
        out.append(dict(mov_id=f"MER-{i + 1:05d}", banco_codigo="612", data=r["data"], tipo=r["tipo"],
                        descricao=r["descricao"], titulo_id=r["documento"], docto_banco="",
                        valor_cents=to_cents(r["valor"]), saldo_cents=to_cents(r["saldo"])))
    for i, r in enumerate(lit):
        h = parse_litoral_historico(r["Histórico"])
        out.append(dict(mov_id=f"LIT-{i + 1:05d}", banco_codigo="437", data=parse_date(r["Data"]).isoformat(),
                        tipo=h["tipo"], descricao=r["Histórico"], titulo_id="", docto_banco=r["Docto"],
                        valor_cents=to_cents(r["Valor (R$)"]), saldo_cents=to_cents(r["Saldo (R$)"])))
    return out


def _n_agenda(raw):
    out = []
    for r in raw:
        d = parse_destino(r["destino_pagamento"])
        out.append(dict(lote_remessa=r["lote_remessa"], banco_pagador=r["banco_pagador"], data_prevista=r["data_prevista"],
                        horario_fechamento=r["horario_fechamento"], titulo_id=r["titulo_id"], fornecedor_id=r["fornecedor_id"],
                        valor_cents=to_cents(r["valor"]), vencimento=r["vencimento"], forma_pagamento=r["forma_pagamento"],
                        destino_tipo=d["tipo"], destino_banco=d.get("banco", ""), destino_agencia=d.get("agencia", ""),
                        destino_conta=d.get("conta", ""), destino_pix=d.get("chave", ""),
                        destino_pagamento=r["destino_pagamento"]))
    return out


# ------------------------------------------------------------------ camada op_* (mutável)
OP_SCHEMA = """
DROP TABLE IF EXISTS op_titulos; DROP TABLE IF EXISTS op_fornecedores; DROP TABLE IF EXISTS op_lotes;
CREATE TABLE op_titulos AS SELECT titulo_id, fornecedor_id, numero_nf_digitado, oc_id, valor_cents, vencimento,
  forma_pagamento, linha_digitavel, status, lote_remessa, data_pagamento, banco_pagador FROM n_titulos;
ALTER TABLE op_titulos ADD COLUMN bloqueado INTEGER NOT NULL DEFAULT 0;
ALTER TABLE op_titulos ADD COLUMN bloqueio_motivo TEXT;
ALTER TABLE op_titulos ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX ux_op_titulos ON op_titulos(titulo_id);
CREATE TABLE op_fornecedores AS SELECT fornecedor_id, banco, agencia, conta, chave_pix, email_cadastrado,
  telefone_cadastrado, forma_pagamento_padrao FROM n_fornecedores;
ALTER TABLE op_fornecedores ADD COLUMN bloqueado INTEGER NOT NULL DEFAULT 0;
ALTER TABLE op_fornecedores ADD COLUMN bloqueio_motivo TEXT;
ALTER TABLE op_fornecedores ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX ux_op_fornecedores ON op_fornecedores(fornecedor_id);
CREATE TABLE op_lotes AS SELECT lote_remessa, banco_pagador, data_pagamento AS data_prevista, horario_fechamento,
  CASE situacao WHEN 'aguardando fechamento' THEN 'aberto' ELSE 'processado' END AS status
  FROM raw_lotes_remessa;
ALTER TABLE op_lotes ADD COLUMN liberado_por TEXT;
ALTER TABLE op_lotes ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX ux_op_lotes ON op_lotes(lote_remessa);
CREATE INDEX ix_n_titulos_forn ON n_titulos(fornecedor_id, nf_num);
CREATE INDEX ix_n_dup ON n_duplicatas(chave_acesso);
CREATE INDEX ix_n_nf ON n_nf(cnpj_root, nf_num);
CREATE INDEX ix_n_dda ON n_dda(cnpj_beneficiario_root, doc_nf_num);
"""


# ------------------------------------------------------------------ qualidade de dados
def _dq(conn: sqlite3.Connection) -> list[dict]:
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    checks = []

    def add(cid, sev, desc, rows, sample_cols=None):
        sample = [dict(r) if sample_cols is None else {c: r[c] for c in sample_cols} for r in rows[:5]]
        checks.append(dict(check_id=cid, severity=sev, description=desc, count=len(rows),
                           sample=json.dumps(sample, ensure_ascii=False)))

    add("DQ01", "info", "Documentação cita comunicacoes_fornecedores.jsonl, politicas.json e ferramentas.json; "
        "o ZIP entrega CSV. Usados os CSVs.", [])
    add("DQ02", "info", "Comunicações com canal diferente de email (o dicionário cita whatsapp; 0 = arquivo só tem email)",
        q("SELECT comunicacao_id FROM raw_comunicacoes_fornecedores WHERE canal<>'email'"))
    add("DQ03", "sinal", "Boleto: valor codificado na linha digitável diferente do valor do título",
        q("SELECT titulo_id, valor_cents, ld_valor_cents, status FROM n_titulos WHERE ld_valor_cents IS NOT NULL AND ld_valor_cents<>valor_cents"))
    add("DQ04", "sinal", "Boleto: vencimento codificado na linha digitável diferente do vencimento do título",
        q("SELECT titulo_id, vencimento, ld_vencimento, status FROM n_titulos WHERE ld_vencimento<>'' AND ld_vencimento<>vencimento"))
    add("DQ05", "sinal", "Linha digitável com dígito verificador inválido (possível erro de digitação)",
        q("SELECT titulo_id, linha_digitavel, status FROM n_titulos WHERE ld_dv_ok=0"))
    add("DQ06", "sinal", "Título lançado por usuário após a data de desligamento",
        q("SELECT t.titulo_id, t.lancado_por, t.data_lancamento, u.data_desligamento, t.status FROM n_titulos t "
          "JOIN raw_usuarios_sistema u ON u.login=t.lancado_por WHERE u.data_desligamento<>'' "
          "AND substr(t.data_lancamento,1,10)>u.data_desligamento"))
    add("DQ07", "sinal", "Título cita OC inexistente em Compras",
        q("SELECT titulo_id, oc_id, status FROM n_titulos WHERE oc_id<>'' AND oc_id NOT IN (SELECT oc_id FROM n_oc)"))
    add("DQ08", "sinal", "Título com NF sem correspondência no Portal NF-e pela raiz do CNPJ do fornecedor + número",
        q("SELECT t.titulo_id, t.fornecedor_id, t.numero_nf_digitado, t.categoria, t.status FROM n_titulos t "
          "WHERE t.nf_num<>'' AND NOT EXISTS (SELECT 1 FROM n_nf n WHERE n.cnpj_root=t.fornecedor_cnpj_root AND n.nf_num=t.nf_num)"))
    add("DQ09", "erro", "Chave de acesso de NF com dígito verificador inválido",
        q("SELECT chave_acesso FROM n_nf WHERE chave_dv_ok=0"))
    add("DQ10", "erro", "CNPJ de fornecedor com dígito verificador inválido",
        q("SELECT fornecedor_id, cnpj FROM n_fornecedores WHERE cnpj_valido=0"))
    add("DQ11", "info", "Mesma pessoa com mais de um login (identidade deve ser resolvida por pessoa_id)",
        q("SELECT pessoa_id, group_concat(login) AS logins FROM raw_usuarios_sistema WHERE pessoa_id<>'' "
          "GROUP BY pessoa_id HAVING count(*)>1"))
    add("DQ12", "info", "Título agendado com vencimento anterior à data prevista do lote (pagamento após vencimento)",
        q("SELECT titulo_id, vencimento, data_prevista FROM n_agenda WHERE vencimento<data_prevista"))
    add("DQ13", "info", "Extrato do Banco Litoral não traz titulo_id: conciliação exige casamento por valor/data/nome",
        q("SELECT mov_id FROM n_extrato WHERE banco_codigo='437' AND tipo LIKE 'PAGTO%'"))
    add("DQ14", "sinal", "Mais de um título não cancelado com mesma raiz de CNPJ + NF normalizada + parcela",
        q("SELECT fornecedor_cnpj_root, nf_num, parcela_n, parcela_total, group_concat(titulo_id) AS titulos, count(*) AS n "
          "FROM n_titulos WHERE status<>'cancelado' AND nf_num<>'' GROUP BY 1,2,3,4 HAVING count(*)>1"))
    add("DQ15", "sinal", "Destino TED/PIX da agenda diferente do cadastro atual do fornecedor",
        q("SELECT a.titulo_id, a.destino_pagamento, f.banco, f.agencia, f.conta, f.chave_pix FROM n_agenda a "
          "JOIN n_fornecedores f USING(fornecedor_id) WHERE (a.destino_tipo='ted' AND (a.destino_banco<>f.banco OR "
          "a.destino_agencia<>f.agencia OR a.destino_conta<>f.conta)) OR (a.destino_tipo='pix' AND a.destino_pix<>f.chave_pix)"))
    add("DQ16", "sinal", "Alteração de dados bancários/pix sem confirmação registrada",
        q("SELECT alteracao_id, fornecedor_id, data_hora, campo, origem FROM raw_alteracoes_cadastro "
          "WHERE campo IN ('banco','agencia','conta','chave_pix') AND confirmacao_registrada<>'sim'"))
    add("DQ17", "info", "Agenda e lotes cobrem até 13/11; a simulação termina em 09/11 08:00 (lotes posteriores avaliados no estado dessa hora)",
        q("SELECT lote_remessa FROM op_lotes WHERE status='aberto' AND data_prevista>='2026-11-09'"))
    return checks


def ingest(conn: sqlite3.Connection, raw_dir: Path, sim_start_iso: str, calendar_out: list | None = None) -> dict:
    raw_dir = Path(raw_dir)
    missing = [f for f in EXPECTED_FILES if not (raw_dir / f).exists()]
    if missing:
        raise FileNotFoundError(f"arquivos ausentes no diretório de dados: {missing}")

    hashes_before = {f: sha256(raw_dir / f) for f in EXPECTED_FILES}
    tables: dict[str, list[dict]] = {}
    with transaction(conn):
        conn.execute("DELETE FROM ingest_manifest")
        for f in EXPECTED_FILES:
            cols, rows = read_csv(raw_dir / f)
            tname = "raw_" + f[:-4]
            _create_table(conn, tname, rows, columns=cols)
            tables[f] = rows
            conn.execute("INSERT INTO ingest_manifest VALUES(?,?,?,?,?)", (f, hashes_before[f], len(rows), tname, sim_start_iso))

        nforn = _n_fornecedores(tables["fornecedores.csv"])
        _create_table(conn, "n_fornecedores", nforn, pk="fornecedor_id")
        _create_table(conn, "n_titulos", _n_titulos(tables["contas_a_pagar.csv"], {f["fornecedor_id"]: f for f in nforn}), pk="titulo_id")
        _create_table(conn, "n_nf", _n_nf(tables["notas_fiscais.csv"]), pk="chave_acesso")
        _create_table(conn, "n_duplicatas", [dict(chave_acesso=r["chave_acesso"], n_dup=int(r["n_dup"]), vencimento=r["vencimento"],
                                                  valor_cents=to_cents(r["valor"])) for r in tables["nf_duplicatas.csv"]])
        _create_table(conn, "n_oc", _n_oc(tables["pedidos_compra.csv"]), pk="oc_id")
        _create_table(conn, "n_dda", _n_dda(tables["dda_boletos.csv"]), pk="dda_id")
        _create_table(conn, "n_extrato", _n_extrato(tables["extrato_banco_meridiano.csv"], tables["extrato_banco_litoral.csv"]), pk="mov_id")
        _create_table(conn, "n_agenda", _n_agenda(tables["agenda_remessa.csv"]), pk="titulo_id")
        for stmt in [s.strip() for s in OP_SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)

        conn.execute("DELETE FROM data_quality")
        dq = _dq(conn)
        conn.executemany("INSERT INTO data_quality VALUES(:check_id,:severity,:description,:count,:sample)", dq)

        # eventos iniciais (idempotentes)
        events.emit(conn, type_=events.EventType.snapshot_carregado, source="ingest", sim_ts=sim_start_iso,
                    payload={"files": len(EXPECTED_FILES)}, trusted=True)
        for lote in conn.execute("SELECT * FROM op_lotes WHERE status='aberto'"):
            events.emit(conn, type_=events.EventType.lote_agendado, source="erp", sim_ts=sim_start_iso,
                        entity_id=lote["lote_remessa"], trusted=True,
                        payload={"data_prevista": lote["data_prevista"], "fechamento": lote["horario_fechamento"],
                                 "banco": lote["banco_pagador"]})
        for c in tables["comunicacoes_fornecedores.csv"]:
            # backlog anterior ao início: conteúdo de terceiros, nunca instrução (trusted=False)
            events.emit(conn, type_=events.EventType.comunicacao_recebida, source=c["canal"],
                        sim_ts=c["data_hora"].replace(" ", "T"), entity_id=c["comunicacao_id"], trusted=False,
                        payload={"comunicacao_id": c["comunicacao_id"], "remetente": c["remetente"], "assunto": c["assunto"]})
        set_state(conn, "ingested", "1")
        set_state(conn, "sim_tick", "0")

    hashes_after = {f: sha256(raw_dir / f) for f in EXPECTED_FILES}
    if hashes_after != hashes_before:
        raise RuntimeError("CSV original foi alterado durante a ingestão")
    summary = {"files": len(EXPECTED_FILES), "rows": {f: len(r) for f, r in tables.items()},
               "dq": {d["check_id"]: d["count"] for d in dq}}
    log(LOG, "ingest concluída", files=len(EXPECTED_FILES), dq=summary["dq"])
    return summary


def load_calendar(conn: sqlite3.Connection) -> BankCalendar:
    hol = {parse_date(r["data"]) for r in conn.execute("SELECT data FROM raw_feriados_bancarios WHERE sem_expediente_bancario='sim'")}
    return BankCalendar(hol)


def table_counts(conn: sqlite3.Connection) -> Counter:
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return Counter({n: conn.execute(f"SELECT count(*) FROM {_q(n)}").fetchone()[0] for n in names})
