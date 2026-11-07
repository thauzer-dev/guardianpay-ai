"""Trilha de auditoria append-only com encadeamento por hash (detecção de adulteração)."""
from __future__ import annotations

import hashlib
import json
import sqlite3

GENESIS = "0" * 64


def _digest(prev_hash: str, record: dict) -> str:
    body = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def append(conn: sqlite3.Connection, *, sim_ts: str, actor: str, actor_role: str, action: str,
           decision: str, payload: dict, target: str | None = None, idempotency_key: str | None = None) -> int:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    record = dict(sim_ts=sim_ts, actor=actor, actor_role=actor_role, action=action, target=target,
                  idempotency_key=idempotency_key, decision=decision, payload=payload)
    h = _digest(prev, record)
    cur = conn.execute(
        "INSERT INTO audit_log(sim_ts,actor,actor_role,action,target,idempotency_key,decision,payload,prev_hash,hash)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sim_ts, actor, actor_role, action, target, idempotency_key, decision,
         json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str), prev, h),
    )
    return cur.lastrowid


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Recalcula a cadeia. Retorna (ok, seq_do_primeiro_registro_inválido)."""
    prev = GENESIS
    for r in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        record = dict(sim_ts=r["sim_ts"], actor=r["actor"], actor_role=r["actor_role"], action=r["action"],
                      target=r["target"], idempotency_key=r["idempotency_key"], decision=r["decision"],
                      payload=json.loads(r["payload"]))
        if r["prev_hash"] != prev or _digest(prev, record) != r["hash"]:
            return False, r["seq"]
        prev = r["hash"]
    return True, None
