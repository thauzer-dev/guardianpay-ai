"""Barramento de eventos persistido. Emissão idempotente (event_id determinístico)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import Enum


class EventType(str, Enum):
    snapshot_carregado = "snapshot.carregado"
    lote_agendado = "lote.agendado"              # lote aberto com prazo de fechamento
    lote_fechamento_proximo = "lote.fechamento_proximo"
    comunicacao_recebida = "comunicacao.recebida"
    titulo_lancado = "titulo.lancado"
    tique = "clock.tique"
    acao_executada = "acao.executada"
    acao_falhou = "acao.falhou"
    aprovacao_respondida = "aprovacao.respondida"


def make_event_id(type_: str, entity_id: str | None, sim_ts: str, discriminator: str = "") -> str:
    raw = f"{type_}|{entity_id or ''}|{sim_ts}|{discriminator}"
    return "EV" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def emit(conn: sqlite3.Connection, *, type_: EventType | str, source: str, sim_ts: str, payload: dict,
         entity_id: str | None = None, trusted: bool = False, discriminator: str = "") -> tuple[str, bool]:
    """Insere o evento se ainda não existir. Retorna (event_id, criado_agora)."""
    t = type_.value if isinstance(type_, EventType) else type_
    eid = make_event_id(t, entity_id, sim_ts, discriminator)
    cur = conn.execute(
        "INSERT OR IGNORE INTO events(event_id,sim_ts,type,source,entity_id,payload,trusted) VALUES(?,?,?,?,?,?,?)",
        (eid, sim_ts, t, source, entity_id, json.dumps(payload, ensure_ascii=False, default=str), int(trusted)),
    )
    return eid, cur.rowcount == 1


def pending(conn: sqlite3.Connection, until_sim_ts: str, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE processed=0 AND sim_ts<=? ORDER BY sim_ts, seq LIMIT ?", (until_sim_ts, limit)
    ).fetchall()


def mark_processed(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("UPDATE events SET processed=1 WHERE event_id=?", (event_id,))
