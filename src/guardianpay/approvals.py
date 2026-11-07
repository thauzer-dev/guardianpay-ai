"""Roteamento de solicitações de aprovação para as pessoas corretas (por papel/pessoa exigidos pela política).

O roteamento é informativo (quem deve decidir); a validade da aprovação continua sendo checada pelo
PolicyEngine no momento de aprovar (segregação, alçada, requisito coberto).
"""
from __future__ import annotations

import hashlib
import json


def candidates(actors, requirement: dict, requester: str) -> list[str]:
    out = []
    for a in actors._actors.values():  # noqa: SLF001 — registro interno, somente leitura
        if a.kind != "human" or not a.active or a.actor_id == requester or a.role != "aprovador":
            continue
        if requirement.get("pessoa_id") and a.pessoa_id != requirement["pessoa_id"]:
            continue
        if requirement.get("role") and requirement["role"] != a.role:
            continue
        out.append(a.actor_id)
    # papel 'aprovador' genérico: prefere quem NÃO é a pessoa nomeada de outro requisito (PS01/PS03)
    return sorted(out, key=lambda x: (bool(actors.get(x).pessoa_id), x))


def route(conn, actors, solicitacao_id: str, sim_ts: str, case_id: str | None = None) -> list[dict]:
    if case_id:
        conn.execute("UPDATE approval_requests SET case_id=coalesce(case_id, ?) WHERE solicitacao_id=?", (case_id, solicitacao_id))
    r = conn.execute("SELECT * FROM approval_requests WHERE solicitacao_id=?", (solicitacao_id,)).fetchone()
    if r is None:
        return []
    routing = [{"requisito": req, "candidatos": candidates(actors, req, r["requested_by"])} for req in json.loads(r["required"])]
    conn.execute("UPDATE approval_requests SET routed_to=? WHERE solicitacao_id=?", (json.dumps(routing), solicitacao_id))
    for i, item in enumerate(routing):
        for who in item["candidatos"][:1]:   # primeiro responsável elegível de cada requisito
            mid = "APR-" + hashlib.sha256(f"{solicitacao_id}:{i}:{who}".encode()).hexdigest()[:12].upper()
            conn.execute("INSERT OR IGNORE INTO outbox(msg_id,channel,to_ref,kind,body,titulos,sim_ts) VALUES(?,?,?,?,?,?,?)",
                         (mid, "interno", who, "aprovacao", f"Aprovação pendente {solicitacao_id} ({r['tool']}) — requisito {item['requisito']}",
                          json.dumps([json.loads(r["params"]).get("titulo_id")] if json.loads(r["params"]).get("titulo_id") else []), sim_ts))
    return routing
