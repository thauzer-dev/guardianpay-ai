import csv
import hashlib
import os
import stat

from conftest import RAW
from guardianpay.ingest import EXPECTED_FILES, SEPARATORS, sha256


def _count(f):
    with open(RAW / f, encoding="utf-8", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh, delimiter=SEPARATORS.get(f, ",")))


def test_all_files_ingested_with_exact_row_counts(rt):
    man = {r["file"]: dict(r) for r in rt.conn.execute("SELECT * FROM ingest_manifest")}
    assert set(man) == set(EXPECTED_FILES) and len(man) == 24
    for f in EXPECTED_FILES:
        assert man[f]["rows"] == _count(f)
        assert rt.conn.execute(f'SELECT count(*) FROM "{man[f]["raw_table"]}"').fetchone()[0] == man[f]["rows"]


def test_raw_files_unchanged_and_readonly(rt):
    for r in rt.conn.execute("SELECT file, sha256 FROM ingest_manifest"):
        assert sha256(RAW / r["file"]) == r["sha256"]
        assert not os.stat(RAW / r["file"]).st_mode & stat.S_IWUSR, "CSV original deve ser somente leitura"


def test_referential_integrity(rt):
    q = lambda s: rt.conn.execute(s).fetchone()[0]  # noqa: E731
    assert q("SELECT count(*) FROM n_titulos WHERE fornecedor_id NOT IN (SELECT fornecedor_id FROM n_fornecedores)") == 0
    assert q("SELECT count(*) FROM n_agenda WHERE titulo_id NOT IN (SELECT titulo_id FROM n_titulos)") == 0
    assert q("SELECT count(*) FROM n_duplicatas WHERE chave_acesso NOT IN (SELECT chave_acesso FROM n_nf)") == 0
    assert q("SELECT count(*) FROM n_agenda a JOIN n_titulos t USING(titulo_id) WHERE a.valor_cents<>t.valor_cents "
             "OR a.lote_remessa<>t.lote_remessa OR t.status<>'agendado'") == 0
    assert q("SELECT count(DISTINCT lote_remessa) FROM n_agenda") == q("SELECT count(*) FROM op_lotes WHERE status='aberto'") == 17


def test_operational_copy_starts_equal_to_original(rt):
    q = lambda s: rt.conn.execute(s).fetchone()[0]  # noqa: E731
    assert q("SELECT count(*) FROM op_titulos") == q("SELECT count(*) FROM raw_contas_a_pagar") == 7120
    assert q("SELECT count(*) FROM op_titulos o JOIN n_titulos n USING(titulo_id) WHERE o.status<>n.status "
             "OR o.valor_cents<>n.valor_cents OR o.lote_remessa<>n.lote_remessa") == 0
    assert q("SELECT count(*) FROM audit_log") == 0


def test_extratos_unified_and_balance_consistent(rt):
    for bank in ("612", "437"):
        rows = rt.conn.execute("SELECT valor_cents, saldo_cents FROM n_extrato WHERE banco_codigo=? ORDER BY mov_id", (bank,)).fetchall()
        assert rows
        for prev, cur in zip(rows, rows[1:]):
            assert prev["saldo_cents"] + cur["valor_cents"] == cur["saldo_cents"]


def test_ingest_is_deterministic(rt, template_db, tmp_path):
    from guardianpay.config import load_settings
    from guardianpay.runtime import bootstrap
    rt2 = bootstrap(load_settings(db_path=tmp_path / "again.db", raw_dir=RAW), reingest=True)

    def digest(conn, t):
        h = hashlib.sha256()
        for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1"):
            h.update(repr(tuple(r)).encode())
        return h.hexdigest()

    for t in ("n_titulos", "n_nf", "n_dda", "n_extrato", "n_agenda", "op_titulos", "data_quality"):
        assert digest(rt.conn, t) == digest(rt2.conn, t)


def test_data_quality_report_present(rt):
    dq = {r["check_id"]: r["count"] for r in rt.conn.execute("SELECT * FROM data_quality")}
    assert dq["DQ09"] == 0 and dq["DQ10"] == 0  # integridade de chaves NF-e e CNPJ
    assert dq["DQ03"] > 0                        # sinais registrados, não "corrigidos"


def test_initial_events(rt):
    types = dict(rt.conn.execute("SELECT type, count(*) FROM events GROUP BY type").fetchall())
    assert types["lote.agendado"] == 17 and types["comunicacao.recebida"] == 246 and types["snapshot.carregado"] == 1
    assert rt.conn.execute("SELECT count(*) FROM events WHERE type='comunicacao.recebida' AND trusted=1").fetchone()[0] == 0
