"""Carrega politicas.csv para uma estrutura tipada e consultável.

O arquivo é a fonte da verdade: nenhum limite numérico é codificado à parte. Os textos de regra
(ex.: troca de conta) são preservados literalmente para evidência e para o motor de políticas.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel


class Pessoa(BaseModel):
    pessoa_id: str
    nome: str
    cargo: str
    acionar_quando: str


def _coerce(v: str):
    s = v.strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _set_path(root: dict, path: str, value) -> None:
    """'bancos_pagadores.Banco Meridiano.codigo' e 'pessoas[1].nome' -> dict aninhado."""
    cur = root
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    for i, tok in enumerate(tokens):
        last = i == len(tokens) - 1
        if tok.startswith("["):
            continue
        nxt_is_idx = not last and tokens[i + 1].startswith("[")
        if nxt_is_idx:
            idx = int(tokens[i + 1][1:-1])
            lst = cur.setdefault(tok, {})
            cur = lst.setdefault(idx, {})
        elif last:
            cur[tok] = value
        else:
            cur = cur.setdefault(tok, {})


@dataclass
class PolicySet:
    raw_rows: list[dict]
    tree: dict = field(default_factory=dict)

    # ---- acesso tipado aos números que o motor de políticas usa
    def num(self, secao: str, regra: str) -> float:
        for r in self.raw_rows:
            if r["secao"] == secao and r["regra"] == regra:
                return float(r["valor"])
        raise KeyError(f"política ausente: {secao}.{regra}")

    def text(self, secao: str, regra: str) -> str:
        for r in self.raw_rows:
            if r["secao"] == secao and r["regra"] == regra:
                return r["valor"]
        raise KeyError(f"política ausente: {secao}.{regra}")

    @property
    def analista_limite_cents(self) -> int:
        return int(round(self.num("alcadas", "analista_cp.liberar_ou_agendar_ate") * 100))

    @property
    def aprovador_limite_cents(self) -> int:
        return int(round(self.num("alcadas", "aprovador.aprova_ate") * 100))

    @property
    def custo_acionamento_desnecessario_cents(self) -> int:
        return int(round(self.num("acionamento_humano", "custo_por_acionamento_desnecessario") * 100))

    def custo_relacionamento_cents(self, categoria: str) -> int:
        return int(round(self.num("atraso", f"custo_de_relacionamento_por_titulo_atrasado.{categoria}") * 100))

    def pessoas(self) -> dict[str, Pessoa]:
        p = self.tree["acionamento_humano"]["pessoas"]
        return {v["pessoa_id"]: Pessoa(**v) for _, v in sorted(p.items())}

    def bancos_pagadores(self) -> dict[str, dict]:
        return self.tree["remessa"]["bancos_pagadores"]

    def dispensas(self) -> dict[str, set[str]]:
        d = self.tree["tres_vias"]["dispensas"]
        return {"oc": {x.strip() for x in str(d["oc"]).split(",")}, "nf": {x.strip() for x in str(d["nf"]).split(",")}}


def load_policies(path: Path) -> PolicySet:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    tree: dict = {}
    for r in rows:
        _set_path(tree.setdefault(r["secao"], {}), r["regra"], _coerce(r["valor"]))
    return PolicySet(raw_rows=rows, tree=tree)
