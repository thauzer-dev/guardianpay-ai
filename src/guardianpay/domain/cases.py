"""Máquina de estados dos casos com transições explícitas e persistidas.

EVENTO → TRIAGEM → INVESTIGAÇÃO → VALIDAÇÃO → DECISÃO → EXECUÇÃO/AUTORIZAÇÃO → VERIFICAÇÃO → REGISTRO
"""
from __future__ import annotations

import json
import sqlite3
from enum import Enum

from .models import Finding


class CaseState(str, Enum):
    detected = "detected"
    investigating = "investigating"
    awaiting_approval = "awaiting_approval"
    authorized = "authorized"
    executing = "executing"
    resolved = "resolved"
    escalated = "escalated"
    failed = "failed"


S = CaseState
TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    S.detected: frozenset({S.investigating, S.resolved, S.escalated}),            # resolved = descartado na triagem (falso positivo)
    S.investigating: frozenset({S.awaiting_approval, S.authorized, S.resolved, S.escalated}),
    S.awaiting_approval: frozenset({S.authorized, S.investigating, S.escalated}),  # recusa volta para investigação
    S.authorized: frozenset({S.executing}),
    # após execução VERIFICADA o caso pode seguir pendente (aprovação/humano); falha nunca vira resolução
    S.executing: frozenset({S.resolved, S.failed, S.awaiting_approval, S.escalated, S.investigating}),
    S.failed: frozenset({S.executing, S.escalated, S.investigating}),             # retry seguro (idempotente) ou escalar
    S.escalated: frozenset({S.investigating, S.awaiting_approval, S.resolved}),
    S.resolved: frozenset(),
}


class InvalidTransition(RuntimeError):
    pass


class ConcurrentModification(RuntimeError):
    pass


def open_case(conn: sqlite3.Connection, finding: Finding, sim_ts: str, actor: str) -> bool:
    """Cria o caso se não existir (idempotente por case_id). Retorna True se criou."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO cases(case_id,rule_id,state,titulos,severity,exposure_cents,certainty,body,created_sim_ts,updated_sim_ts)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (finding.case_id, finding.rule_id, S.detected.value, json.dumps(finding.titulos), finding.severity.value,
         finding.exposure_cents, finding.certainty.value, finding.model_dump_json(), sim_ts, sim_ts))
    if cur.rowcount:
        conn.execute("INSERT INTO case_transitions(case_id,from_state,to_state,sim_ts,actor,reason) VALUES(?,?,?,?,?,?)",
                     (finding.case_id, None, S.detected.value, sim_ts, actor, "detecção"))
    return cur.rowcount == 1


def transition(conn: sqlite3.Connection, case_id: str, to: CaseState, *, sim_ts: str, actor: str, reason: str,
               expected_version: int | None = None, verified: bool = False) -> int:
    row = conn.execute("SELECT state, version FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise KeyError(case_id)
    cur_state = CaseState(row["state"])
    if to not in TRANSITIONS[cur_state]:
        raise InvalidTransition(f"{case_id}: {cur_state.value} -> {to.value} não permitido")
    if cur_state is S.executing and to is not S.failed and not verified:
        raise InvalidTransition("saída de 'executing' (exceto failed) só após verificação do resultado")
    if expected_version is not None and expected_version != row["version"]:
        raise ConcurrentModification(case_id)
    conn.execute("UPDATE cases SET state=?, updated_sim_ts=?, version=version+1 WHERE case_id=? AND version=?",
                 (to.value, sim_ts, case_id, row["version"]))
    conn.execute("INSERT INTO case_transitions(case_id,from_state,to_state,sim_ts,actor,reason) VALUES(?,?,?,?,?,?)",
                 (case_id, cur_state.value, to.value, sim_ts, actor, reason))
    return row["version"] + 1


def history(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM case_transitions WHERE case_id=? ORDER BY seq", (case_id,))]
