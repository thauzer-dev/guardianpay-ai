import csv

import pytest

from conftest import RAW
from guardianpay.normalize import (
    cnpj_root, cnpj_valid, decode_chave_acesso, decode_linha_digitavel, normalize_nf_number, parse_date,
    parse_destino, parse_litoral_historico, to_cents,
)


@pytest.mark.parametrize("raw,cents", [("1234.56", 123456), ("-4.129,58", -412958), ("50.000,00", 5000000),
                                        ("R$ 30.023,03", 3002303), ("0.10", 10), ("", None)])
def test_money(raw, cents):
    assert to_cents(raw) == cents


@pytest.mark.parametrize("raw,exp", [("000063242", ("63242", None)), ("69686-1", ("69686", "1")),
                                      ("35743/1", ("35743", "1")), ("62523", ("62523", None)), ("", ("", None))])
def test_nf_number(raw, exp):
    assert normalize_nf_number(raw) == exp


def test_dates_br_and_iso():
    assert parse_date("03/08/2026").isoformat() == "2026-08-03" == parse_date("2026-08-03").isoformat()


def test_cnpj():
    assert cnpj_valid("85.779.751/0001-00") and not cnpj_valid("85.779.751/0001-01")
    assert cnpj_root("13.030.916/0002-17") == "13030916"


def test_linha_digitavel_matches_every_dda_record():
    """Fórmula do dicionário validada contra 100% do DDA (banco, valor, vencimento, DVs)."""
    rows = list(csv.DictReader(open(RAW / "dda_boletos.csv", encoding="utf-8")))
    for r in rows:
        d = decode_linha_digitavel(r["linha_digitavel"])
        assert d["banco"] == r["banco_cobrador"]
        assert d["valor_cents"] == to_cents(r["valor"])
        assert d["vencimento"] == r["vencimento"]
        assert d["dv_campos_ok"] and d["dv_geral_ok"]
        assert d["nosso_numero"] == r["nosso_numero"]


def test_linha_digitavel_detects_typo():
    ld = "28995217401010503090100000967141715440002124941"
    bad = ld[:12] + str((int(ld[12]) + 1) % 10) + ld[13:]
    assert decode_linha_digitavel(ld)["dv_campos_ok"]
    assert not decode_linha_digitavel(bad)["dv_campos_ok"]
    assert decode_linha_digitavel("") is None
    assert decode_linha_digitavel("123")["valida_formato"] is False


def test_chave_acesso_all_valid():
    for r in csv.DictReader(open(RAW / "notas_fiscais.csv", encoding="utf-8")):
        k = decode_chave_acesso(r["chave_acesso"])
        assert k["dv_ok"] and k["cnpj_emitente"] == r["cnpj_emitente"]


def test_destino_and_litoral():
    assert parse_destino("ted 551/9119/3967423-4") == {"tipo": "ted", "banco": "551", "agencia": "9119", "conta": "3967423-4"}
    assert parse_destino("pix 15701848000115")["chave"] == "15701848000115"
    assert parse_destino("boleto banco 289")["banco"] == "289"
    assert parse_litoral_historico("TED ENV 289 STUDIO CLARO FOTOG") == {"tipo": "PAGTO TED", "banco": "289", "favorecido": "STUDIO CLARO FOTOG"}
    assert parse_litoral_historico("PAG BOL 289 X")["tipo"] == "PAGTO BOLETO"
    assert parse_litoral_historico("TAR PAGTO LOTE")["tipo"] == "TARIFA"
