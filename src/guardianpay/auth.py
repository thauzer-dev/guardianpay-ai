"""Sessões autenticadas para a futura API.

O cliente NUNCA informa actor_id: apresenta um token opaco; o servidor resolve a identidade pela sessão
(emitida após autenticação real — SSO/senha — fora deste módulo). Tokens são guardados apenas como hash.
Agentes não recebem sessões externas: rodam no processo confiável com identidades fixas.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from .identity import ActorRegistry
from .tools.contract import ToolRequest, ToolResult, ToolStatus

SESSION_TTL = timedelta(hours=12)


class AuthError(PermissionError):
    pass


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionManager:
    def __init__(self, conn, clock, actors: ActorRegistry, *, demo_mode: bool = False, now_fn=None):
        # validade da sessão usa o relógio REAL (UTC), não o simulado: avançar a simulação não derruba usuários
        from datetime import datetime, timezone
        self.conn, self.clock, self.actors, self.demo_mode = conn, clock, actors, demo_mode
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def issue_demo(self, actor_id: str, *, purpose: str) -> str:
        """Login simulado por identificador: SÓ em modo demonstração (GP_DEMO_MODE=1). Fica marcado na sessão."""
        if not self.demo_mode:
            raise AuthError("login simulado desabilitado fora do modo demonstração")
        return self._issue(actor_id, issued_by=f"demo:{purpose}")

    def issue_from_identity_provider(self, id_token: str, verifier) -> str:
        """Caminho da futura API: o IdP (SSO/OIDC) autentica; `verifier` valida assinatura, emissor, audiência e
        validade e devolve as claims. A identidade vem do e-mail corporativo verificado, nunca do frontend."""
        try:
            claims = verifier(id_token)
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"token do provedor inválido: {type(e).__name__}") from None
        if not isinstance(claims, dict) or not claims.get("email") or not claims.get("email_verified", False):
            raise AuthError("claims sem e-mail verificado")
        rows = self.conn.execute("SELECT login FROM raw_usuarios_sistema WHERE lower(email)=lower(?) AND sistema='ERP'",
                                 (claims["email"],)).fetchall()
        if len(rows) != 1:
            raise AuthError("e-mail não mapeia para exatamente um usuário do ERP")
        return self._issue(rows[0]["login"], issued_by=f"idp:{claims.get('iss', '?')}")

    def issue(self, actor_id: str, *, issued_by: str) -> str:
        """Compatibilidade: só aceita emissão de demonstração (use issue_demo / issue_from_identity_provider)."""
        return self.issue_demo(actor_id, purpose=issued_by)

    def _issue(self, actor_id: str, *, issued_by: str) -> str:
        try:
            a = self.actors.get(actor_id)
        except LookupError:
            raise AuthError("identidade desconhecida") from None
        if a.kind != "human":
            raise AuthError("sessões externas são só para pessoas")
        if not a.active:
            raise AuthError("usuário inativo")
        token = secrets.token_urlsafe(32)
        now = self._now()
        self.conn.execute("INSERT INTO sessions(token_hash,actor_id,issued_by,issued_sim_ts,expires_sim_ts) VALUES(?,?,?,?,?)",
                          (_h(token), actor_id, issued_by, now.isoformat(), (now + SESSION_TTL).isoformat()))
        return token

    def resolve(self, token: str | None) -> str:
        if not token:
            raise AuthError("não autenticado")
        r = self.conn.execute("SELECT * FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
        if r is None or r["revoked"]:
            raise AuthError("sessão inválida")
        if r["issued_by"].startswith("demo:") and not self.demo_mode:
            raise AuthError("sessão de demonstração não é aceita fora do modo demonstração")
        if r["expires_sim_ts"] <= self._now().isoformat():
            raise AuthError("sessão expirada")
        if not self.actors.get(r["actor_id"]).active:
            raise AuthError("usuário inativo")
        return r["actor_id"]

    def revoke(self, token: str) -> None:
        self.conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (_h(token),))


def execute_with_session(rt, token: str | None, tool: str, params: dict, *, idempotency_key: str | None = None,
                         approval_id: str | None = None) -> ToolResult:
    """Porta de entrada para clientes externos: o ator vem da sessão; qualquer 'actor_id' do cliente é ignorado."""
    try:
        actor_id = rt.sessions.resolve(token)
    except AuthError as e:
        return ToolResult(tool=tool, status=ToolStatus.denied, reasons=[str(e)], idempotency_key=idempotency_key or "")
    clean = {k: v for k, v in params.items() if k not in ("actor_id", "actor", "role", "papel")}
    return rt.executor.execute(ToolRequest(tool=tool, actor_id=actor_id, params=clean, idempotency_key=idempotency_key,
                                           approval_id=approval_id))
