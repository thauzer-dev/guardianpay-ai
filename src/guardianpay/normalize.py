"""Normalização determinística de formatos (dinheiro, datas, CNPJ, NF, boletos, extratos).

Todas as funções são puras: recebem texto original e devolvem valor derivado.
Valores monetários são tratados em centavos (int) para evitar erro de ponto flutuante.
"""
from __future__ import annotations

import re
from functools import lru_cache
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

BOLETO_FATOR_BASE = date(2025, 2, 22)  # fator 1000 (dicionário de dados)


# ---------------------------------------------------------------- dinheiro
def to_cents(value: str | float | Decimal | None) -> int | None:
    """'1234.56' -> 123456 ; '-4.129,58' (formato BR) -> -412958 ; '' -> None."""
    if value is None:
        return None
    s = str(value).strip().replace("R$", "").replace(" ", "")
    if s == "":
        return None
    if "," in s:  # formato BR: ponto = milhar, vírgula = decimal
        s = s.replace(".", "").replace(",", ".")
    try:
        return int((Decimal(s) * 100).quantize(Decimal("1")))
    except InvalidOperation as e:
        raise ValueError(f"valor monetário inválido: {value!r}") from e


def cents_to_str(c: int | None) -> str:
    return "" if c is None else f"{c / 100:.2f}"


# ---------------------------------------------------------------- datas
def parse_date(s: str | None) -> date | None:
    """Aceita ISO (aaaa-mm-dd) e BR (dd/mm/aaaa)."""
    if not s or not str(s).strip():
        return None
    s = str(s).strip()[:10]
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):
        return datetime.strptime(s, "%d/%m/%Y").date()
    return date.fromisoformat(s)


def parse_datetime(s: str | None) -> datetime | None:
    if not s or not str(s).strip():
        return None
    return datetime.fromisoformat(str(s).strip().replace("T", " "))


# ---------------------------------------------------------------- CNPJ
def cnpj_digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def cnpj_root(s: str | None) -> str:
    d = cnpj_digits(s)
    return d[:8] if len(d) == 14 else ""


def cnpj_valid(s: str | None) -> bool:
    d = cnpj_digits(s)
    if len(d) != 14 or d == d[0] * 14:
        return False

    def dv(base: str, weights: list[int]) -> int:
        r = sum(int(x) * w for x, w in zip(base, weights)) % 11
        return 0 if r < 2 else 11 - r

    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    return dv(d[:12], w1) == int(d[12]) and dv(d[:13], w2) == int(d[13])


# ---------------------------------------------------------------- número de NF
_NF_RE = re.compile(r"^\s*0*(\d+)\s*(?:[-/]\s*(\w+))?\s*$")


def normalize_nf_number(raw: str | None) -> tuple[str, str | None]:
    """'000063242' -> ('63242', None); '69686-1' -> ('69686','1'); '35743/1' -> ('35743','1').

    O sufixo após '-' ou '/' é interpretado como série (dicionário de dados). Não sabemos se é
    série ou parcela em todos os casos: o chamador deve tratar a série como evidência fraca.
    """
    if raw is None or not str(raw).strip():
        return ("", None)
    m = _NF_RE.match(str(raw))
    if not m:
        digits = re.sub(r"\D", "", str(raw)).lstrip("0")
        return (digits, None)
    return (m.group(1).lstrip("0") or "0", m.group(2))


# ---------------------------------------------------------------- chave de acesso NF-e
def decode_chave_acesso(chave: str) -> dict:
    c = cnpj_digits(chave)
    if len(c) != 44:
        return {"valida_formato": False}
    weights = [2, 3, 4, 5, 6, 7, 8, 9]
    total = sum(int(d) * weights[i % 8] for i, d in enumerate(reversed(c[:43])))
    r = total % 11
    dv = 0 if r < 2 else 11 - r
    return {
        "valida_formato": True,
        "uf": c[0:2], "aamm": c[2:6], "cnpj_emitente": c[6:20], "modelo": c[20:22],
        "serie": c[22:25].lstrip("0") or "0", "numero": c[25:34].lstrip("0") or "0",
        "tipo_emissao": c[34], "codigo": c[35:43], "dv": int(c[43]), "dv_ok": dv == int(c[43]),
    }


# ---------------------------------------------------------------- boleto
def _mod10(s: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(s)):
        p = int(ch) * (2 if i % 2 == 0 else 1)
        total += p // 10 + p % 10
    return (10 - total % 10) % 10


def _mod11_barcode(s: str) -> int:
    total = sum(int(ch) * (2 + i % 8) for i, ch in enumerate(reversed(s)))
    r = 11 - total % 11
    return 1 if r in (0, 10, 11) else r


def decode_linha_digitavel(ld: str | None) -> dict | None:
    """Versão com cache (função pura; devolve cópia para não expor o objeto em cache)."""
    if ld is None or not str(ld).strip():
        return None
    r = _decode_linha_digitavel_cached(str(ld))
    return dict(r)


@lru_cache(maxsize=65536)
def _decode_linha_digitavel_cached(ld: str) -> dict | None:
    return _decode_linha_digitavel(ld)


def _decode_linha_digitavel(ld: str | None) -> dict | None:
    """Decodifica a linha de 47 dígitos conforme o dicionário de dados.

    Retorna None para vazio. Campos: banco, moeda, agencia, conta, carteira, nosso_numero,
    fator, vencimento, valor_cents, dv_campos_ok, dv_geral_ok.
    """
    if ld is None or not str(ld).strip():
        return None
    d = re.sub(r"\D", "", str(ld))
    if len(d) != 47:
        return {"valida_formato": False, "erro": f"tamanho {len(d)} != 47"}
    campo_livre = d[4:9] + d[10:20] + d[21:31]  # 25 dígitos
    fator = int(d[33:37])
    barcode_sem_dv = d[0:4] + d[33:47] + campo_livre
    return {
        "valida_formato": True,
        "banco": d[0:3],
        "moeda": d[3],
        "agencia": campo_livre[0:4],
        "conta": campo_livre[4:12],
        "carteira": campo_livre[12:14],
        "nosso_numero": campo_livre[14:25],
        "fator": fator,
        "vencimento": (BOLETO_FATOR_BASE + timedelta(days=fator - 1000)).isoformat(),
        "valor_cents": int(d[37:47]),
        "dv_campos_ok": _mod10(d[0:9]) == int(d[9]) and _mod10(d[10:20]) == int(d[20]) and _mod10(d[21:31]) == int(d[31]),
        "dv_geral_ok": _mod11_barcode(barcode_sem_dv) == int(d[32]),
    }


# ---------------------------------------------------------------- destino de pagamento (agenda)
def parse_destino(destino: str | None) -> dict:
    """'ted 551/9119/3967423-4' | 'pix <chave>' | 'boleto banco 289'."""
    s = (destino or "").strip()
    if s.startswith("ted "):
        parts = s[4:].split("/")
        if len(parts) == 3:
            return {"tipo": "ted", "banco": parts[0], "agencia": parts[1], "conta": parts[2]}
    if s.startswith("pix "):
        return {"tipo": "pix", "chave": s[4:].strip()}
    m = re.match(r"boleto banco (\d{3})", s)
    if m:
        return {"tipo": "boleto", "banco": m.group(1)}
    return {"tipo": "desconhecido", "raw": s}


# ---------------------------------------------------------------- extratos
_LIT_PATTERNS = [
    (re.compile(r"^PIX ENV (\S+) (.+)$"), "PAGTO PIX", ("chave_ref", "favorecido")),
    (re.compile(r"^TED ENV (\d{3}) (.+)$"), "PAGTO TED", ("banco", "favorecido")),
    (re.compile(r"^PAG BOL (\d{3}) (.+)$"), "PAGTO BOLETO", ("banco", "favorecido")),
    (re.compile(r"^TAR (.+)$"), "TARIFA", ("descricao",)),
    (re.compile(r"^CRED (.+)$"), "CREDITO", ("descricao",)),
]


def parse_litoral_historico(h: str) -> dict:
    """Converte o 'Histórico' livre do Banco Litoral para o vocabulário de tipos do Meridiano."""
    s = (h or "").strip()
    for rx, tipo, names in _LIT_PATTERNS:
        m = rx.match(s)
        if m:
            out = {"tipo": tipo}
            out.update(dict(zip(names, m.groups())))
            return out
    return {"tipo": "OUTRO", "descricao": s}


def normalize_name(s: str | None) -> str:
    """Nome em maiúsculas sem acento/pontuação, para comparação aproximada entre sistemas."""
    import unicodedata

    t = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9 ]", " ", t).upper()
    return re.sub(r"\s+", " ", t).strip()
