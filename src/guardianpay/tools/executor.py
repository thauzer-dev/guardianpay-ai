"""Executor único de ferramentas: política -> idempotência -> efeito (savepoint) -> verificação -> auditoria.

Garantias:
* Nenhuma mutação sem decisão 'allow' do PolicyEngine.
* Mesma idempotency_key nunca executa duas vezes (resultado é reproduzido do registro).
* Efeito e registro são atômicos: crash no meio = rollback = retry seguro.
* Falha de execução ou de verificação => status failed (nunca vira sucesso/resolução).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .. import audit, events
from ..clock import SimClock
from ..identity import ActorRegistry, UnknownActor
from ..log import get_logger, log
from ..policy_engine import Outcome, PolicyEngine
from ..store import transaction
from .catalog import ToolKind, ToolSpec
from .contract import ToolRequest, ToolResult, ToolStatus

LOG = get_logger("executor")


class VerificationError(RuntimeError):
    pass


@dataclass
class Handler:
    run: Callable[["ExecContext"], Any]
    verify: Callable[["ExecContext", Any], bool] | None = None
    provenance: str = "executado"


@dataclass
class ExecContext:
    conn: sqlite3.Connection
    req: ToolRequest
    actor: Any
    sim_ts: str
    engine: PolicyEngine
    spec: ToolSpec


class ToolExecutor:
    def __init__(self, conn: sqlite3.Connection, catalog: dict[str, ToolSpec], engine: PolicyEngine,
                 actors: ActorRegistry, clock: SimClock, handlers: dict[str, Handler]):
        self.conn, self.catalog, self.engine, self.actors, self.clock, self.handlers = conn, catalog, engine, actors, clock, handlers

    @property
    def sim_ts(self) -> str:
        return self.clock.now.isoformat()

    def _role(self, actor_id: str) -> str:
        try:
            return self.actors.get(actor_id).role
        except UnknownActor:
            return "desconhecido"

    def execute(self, req: ToolRequest) -> ToolResult:
        spec = self.catalog.get(req.tool)
        if spec is not None and spec.kind is ToolKind.leitura:
            return self._read(req, spec)
        return self._mutate(req, spec)

    # ------------------------------------------------------------------ leitura
    def _read(self, req: ToolRequest, spec: ToolSpec) -> ToolResult:
        key = req.effective_key()
        d = self.engine.evaluate(req, self.sim_ts)
        if d.outcome is not Outcome.allow:
            return ToolResult(tool=req.tool, status=ToolStatus.denied, reasons=d.reasons, idempotency_key=key)
        h = self.handlers.get(req.tool)
        if h is None:
            return ToolResult(tool=req.tool, status=ToolStatus.not_implemented, idempotency_key=key)
        ctx = ExecContext(self.conn, req, self.actors.get(req.actor_id), self.sim_ts, self.engine, spec)
        return ToolResult(tool=req.tool, status=ToolStatus.ok, data=h.run(ctx), idempotency_key=key, provenance="original")

    # ------------------------------------------------------------------ mutação
    def _mutate(self, req: ToolRequest, spec: ToolSpec | None) -> ToolResult:
        key, phash = req.effective_key(), req.params_hash()
        prior = self.conn.execute("SELECT * FROM tool_calls WHERE idempotency_key=?", (key,)).fetchone()
        if prior is not None:
            if prior["params_hash"] != phash or prior["tool"] != req.tool:
                return ToolResult(tool=req.tool, status=ToolStatus.denied, idempotency_key=key,
                                  reasons=["idempotency_key reutilizada com parâmetros diferentes"])
            # só efeitos concluídos são reproduzidos; negação/falha dependem de estado e podem ser reavaliadas
            if prior["status"] in ("succeeded", "pending_approval"):
                stored = ToolResult.model_validate_json(prior["result"])
                return stored.model_copy(update={"replayed": True})

        with transaction(self.conn):
            role = self._role(req.actor_id)
            decision = self.engine.evaluate(req, self.sim_ts)
            base = dict(sim_ts=self.sim_ts, actor=req.actor_id, actor_role=role, action=req.tool,
                        target=str(req.params.get("titulo_id") or req.params.get("fornecedor_id") or req.params.get("solicitacao_id") or ""),
                        idempotency_key=key)

            if decision.outcome is Outcome.deny:
                seq = audit.append(self.conn, decision="denied", payload={"params": req.params, "reasons": decision.reasons}, **base)
                res = ToolResult(tool=req.tool, status=ToolStatus.denied, reasons=decision.reasons, audit_seq=seq, idempotency_key=key)
                return self._record(key, req, phash, "denied", res, prior)

            if decision.outcome is Outcome.require_approval:
                sid = "SOL-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
                params = dict(req.params, _amount_cents=decision.amount_cents)
                ev = req.params.get("evidencias") or []
                self.conn.execute(
                    "INSERT OR IGNORE INTO approval_requests(solicitacao_id,tool,params,requested_by,required,status,evidence,created_sim_ts,updated_sim_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (sid, req.tool, json.dumps(params, sort_keys=True, default=str), req.actor_id, json.dumps(decision.required),
                     "pending", json.dumps(ev, default=str), self.sim_ts, self.sim_ts))
                from ..approvals import route
                route(self.conn, self.actors, sid, self.sim_ts, req.case_id)
                seq = audit.append(self.conn, decision="requires_approval",
                                   payload={"params": req.params, "required": decision.required, "solicitacao_id": sid, "reasons": decision.reasons}, **base)
                res = ToolResult(tool=req.tool, status=ToolStatus.pending_approval, reasons=decision.reasons, solicitacao_id=sid,
                                 audit_seq=seq, idempotency_key=key, data={"required": decision.required})
                return self._record(key, req, phash, "pending_approval", res, prior)

            handler = self.handlers.get(req.tool)
            if handler is None:
                seq = audit.append(self.conn, decision="failed", payload={"params": req.params, "error": "not_implemented"}, **base)
                res = ToolResult(tool=req.tool, status=ToolStatus.not_implemented, audit_seq=seq, idempotency_key=key,
                                 reasons=["adaptador ainda não implementado"])
                return self._record(key, req, phash, "failed", res, prior)

            ctx = ExecContext(self.conn, req, self.actors.get(req.actor_id), self.sim_ts, self.engine, spec)
            self.conn.execute("SAVEPOINT tool_effect")
            try:
                data = handler.run(ctx)
                if handler.verify is not None and not handler.verify(ctx, data):
                    raise VerificationError("estado após a execução não confere com o esperado")
                if req.approval_id:
                    self.conn.execute("UPDATE approval_requests SET status='executed', updated_sim_ts=? WHERE solicitacao_id=?",
                                      (self.sim_ts, req.approval_id))
                self.conn.execute("RELEASE tool_effect")
            except Exception as e:  # noqa: BLE001 — qualquer falha vira 'failed' auditado
                self.conn.execute("ROLLBACK TO tool_effect")
                self.conn.execute("RELEASE tool_effect")
                seq = audit.append(self.conn, decision="failed", payload={"params": req.params, "error": f"{type(e).__name__}: {e}"}, **base)
                events.emit(self.conn, type_=events.EventType.acao_falhou, source="executor", sim_ts=self.sim_ts,
                            entity_id=key, trusted=True, payload={"tool": req.tool, "error": str(e)}, discriminator=str(seq))
                log(LOG, "ferramenta falhou", tool=req.tool, key=key, error=str(e))
                res = ToolResult(tool=req.tool, status=ToolStatus.failed, reasons=[str(e)], audit_seq=seq, idempotency_key=key)
                return self._record(key, req, phash, "failed", res, prior)

            seq = audit.append(self.conn, decision="executed", payload={"params": req.params, "result": data,
                               "approval_id": req.approval_id, "provenance": handler.provenance}, **base)
            events.emit(self.conn, type_=events.EventType.acao_executada, source="executor", sim_ts=self.sim_ts,
                        entity_id=key, trusted=True, payload={"tool": req.tool, "case_id": req.case_id})
            res = ToolResult(tool=req.tool, status=ToolStatus.ok, data=data, audit_seq=seq, idempotency_key=key,
                             solicitacao_id=req.approval_id, provenance=handler.provenance)
            log(LOG, "ferramenta executada", tool=req.tool, actor=req.actor_id, key=key)
            return self._record(key, req, phash, "succeeded", res, prior)

    def _record(self, key, req, phash, status, res: ToolResult, prior) -> ToolResult:
        if prior is None:
            self.conn.execute("INSERT INTO tool_calls(idempotency_key,tool,actor,params_hash,status,result,sim_ts) VALUES(?,?,?,?,?,?,?)",
                              (key, req.tool, req.actor_id, phash, status, res.model_dump_json(), self.sim_ts))
        else:
            self.conn.execute("UPDATE tool_calls SET status=?, result=?, sim_ts=?, attempts=attempts+1 WHERE idempotency_key=?",
                              (status, res.model_dump_json(), self.sim_ts, key))
        return res
