"""Identidades e papéis. O papel vem SEMPRE do registro, nunca da requisição.

Agentes têm identidades fixas com papéis do catálogo limitados a analista_cp e auditor:
nenhum agente pode receber o papel 'aprovador' (não há caminho de código para isso).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

AGENT_ROLES_ALLOWED = frozenset({"analista_cp", "auditor"})

# perfil do ERP -> papel do catálogo de ferramentas (decisão documentada em PROJECT_STATE.md)
PERFIL_TO_ROLE = {
    "analista_cp": "analista_cp",
    "aprovador": "aprovador",
    "diretoria": "aprovador",
    "tesouraria": "aprovador",   # tesoureiro assina lote hoje; alçada 50 mil em usuarios_sistema
    "auditor": "auditor",
}

AGENTS = {
    "agent:orchestrator": "analista_cp",
    "agent:financial_audit": "auditor",
    "agent:risk_fraud": "auditor",
    "agent:treasury": "analista_cp",
    "agent:approval_collab": "analista_cp",
}


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    kind: str                    # agent | human
    pessoa_id: str = ""
    nome: str = ""
    alcada_cents: int | None = 0  # None = sem limite
    active: bool = True


class UnknownActor(LookupError):
    pass


class ActorRegistry:
    def __init__(self, actors: dict[str, Actor]):
        for a in actors.values():
            if a.kind == "agent" and a.role not in AGENT_ROLES_ALLOWED:
                raise ValueError(f"agente {a.actor_id} não pode ter papel {a.role}")
        self._actors = actors

    def get(self, actor_id: str) -> Actor:
        try:
            return self._actors[actor_id]
        except KeyError as e:
            raise UnknownActor(actor_id) from e

    def by_pessoa(self, pessoa_id: str) -> list[Actor]:
        return [a for a in self._actors.values() if a.pessoa_id == pessoa_id]

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "ActorRegistry":
        actors: dict[str, Actor] = {
            aid: Actor(actor_id=aid, role=role, kind="agent", nome=aid,
                       alcada_cents=500_000 if role == "analista_cp" else 0)
            for aid, role in AGENTS.items()
        }
        for u in conn.execute("SELECT * FROM raw_usuarios_sistema WHERE sistema='ERP'"):
            role = PERFIL_TO_ROLE.get(u["perfil"])
            if not role:
                continue
            alc = None if u["alcada_rs"] == "sem limite" else int(round(float(u["alcada_rs"]) * 100))
            actors[u["login"]] = Actor(actor_id=u["login"], role=role, kind="human", pessoa_id=u["pessoa_id"],
                                       nome=u["nome"], alcada_cents=alc, active=u["status"] == "ativo")
        return cls(actors)
