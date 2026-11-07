"""Contrato comum para ferramentas de consulta e de mutação."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(str, Enum):
    ok = "ok"                              # leitura concluída ou mutação executada E verificada
    denied = "denied"                      # bloqueada pelo motor de políticas
    pending_approval = "pending_approval"  # registrada como solicitação; nada foi executado
    failed = "failed"                      # erro de execução ou verificação; NÃO é resolução
    not_implemented = "not_implemented"    # ferramenta do catálogo ainda sem adaptador


class ToolRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    tool: str
    actor_id: str                                   # identidade explícita; papel resolvido pelo registro
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    approval_id: str | None = None                  # solicitação aprovada que autoriza a execução
    case_id: str | None = None

    def params_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.params, sort_keys=True, default=str).encode()).hexdigest()

    def effective_key(self) -> str:
        if self.idempotency_key:  # a execução autorizada é uma operação distinta da solicitação
            return self.idempotency_key + (f"#{self.approval_id}" if self.approval_id else "")
        return "auto-" + hashlib.sha256(f"{self.tool}|{self.actor_id}|{self.params_hash()}|{self.approval_id}".encode()).hexdigest()[:24]


class ToolResult(BaseModel):
    tool: str
    status: ToolStatus
    data: Any = None
    reasons: list[str] = []
    solicitacao_id: str | None = None
    audit_seq: int | None = None
    idempotency_key: str
    replayed: bool = False                          # True = resultado devolvido do registro, sem reexecutar
    provenance: str = "executado"                   # executado (estado op_*) | simulado (efeito externo fictício)
