"""Catálogo de ferramentas carregado de ferramentas.csv (fonte da verdade de papéis e parâmetros)."""
from __future__ import annotations

import csv
import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class ToolKind(str, Enum):
    leitura = "leitura"
    execucao = "execucao"
    investigacao = "investigacao"
    aprovacao = "aprovacao"
    comunicacao = "comunicacao"

    @property
    def is_mutation(self) -> bool:
        return self is not ToolKind.leitura


class ToolSpec(BaseModel):
    name: str
    kind: ToolKind
    roles: frozenset[str]
    description: str
    params_required: tuple[str, ...]
    params_optional: tuple[str, ...]
    limits: dict
    limits_raw: str
    cost_raw: str

    @property
    def all_params(self) -> set[str]:
        return set(self.params_required) | set(self.params_optional)


def load_catalog(path: Path) -> dict[str, ToolSpec]:
    out: dict[str, ToolSpec] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            params = [p.strip() for p in r["parametros"].split(";") if p.strip()]
            try:
                limits = json.loads(r["limites"]) if r["limites"].strip() else {}
            except json.JSONDecodeError:
                limits = {"_texto": r["limites"]}
            out[r["ferramenta"]] = ToolSpec(
                name=r["ferramenta"],
                kind=ToolKind(r["tipo"]),
                roles=frozenset(x.strip() for x in r["papeis_permitidos"].split(";") if x.strip()),
                description=r["descricao"],
                params_required=tuple(p[:-1] for p in params if p.endswith("*")),
                params_optional=tuple(p for p in params if not p.endswith("*")),
                limits=limits,
                limits_raw=r["limites"],
                cost_raw=r["custo_rs"],
            )
    return out
