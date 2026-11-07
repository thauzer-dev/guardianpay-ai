"""API HTTP do GuardianPay (FastAPI) — reutiliza Runtime, Orchestrator, PolicyEngine e ToolExecutor.

Segurança:
* Toda rota de instância exige `Authorization: Bearer <token>` de sessão emitida no servidor; o ator é resolvido
  pela sessão daquela instância (sessões não valem entre instâncias). Nenhum `actor_id` do cliente é aceito.
* Login por identificador só existe em modo demonstração (GP_DEMO_MODE=1) e as sessões ficam marcadas `demo:`.
  Em produção: `/auth/sso` com verificador do provedor de identidade (não configurado nesta entrega → 501).
* Mutação financeira só via ToolExecutor (PolicyEngine + idempotência + auditoria). Comandos de simulação exigem
  `Idempotency-Key` e são serializados pelo lock da instância.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..audit import verify_chain
from ..auth import AuthError, execute_with_session
from ..config import ROOT, Settings, load_settings
from ..llm.proxy_client import ProxyClient
from ..normalize import cents_to_str
from ..sim.scoreboard import compute
from ..store import get_state
from ..tools.contract import ToolRequest
from .agent_chat import AgentChat
from .instances import Instance, InstanceBusy, InstanceManager


# ------------------------------------------------------------------ contratos
class CreateInstance(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    approval_simulator: bool = False
    llm_mode: str | None = Field(default=None, pattern="^(auto|off)$")


class DemoLogin(BaseModel):
    login: str = Field(min_length=3, max_length=40)


class SSOLogin(BaseModel):
    id_token: str


class Decide(BaseModel):
    comentario: str = Field(default="", max_length=500)


class Reject(BaseModel):
    motivo: str = Field(min_length=3, max_length=500)


class Advance(BaseModel):
    hours: int = Field(ge=1, le=168)


class Resume(BaseModel):
    interval_s: float = Field(default=1.5, ge=0.2, le=30)


class Toggle(BaseModel):
    enabled: bool


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def create_app(settings: Settings | None = None, *, instances_root: Path | None = None, template_root: Path | None = None,
               local_hosts: set[str] | None = None) -> FastAPI:
    s = settings or load_settings()
    mgr = InstanceManager(s, instances_root, template_root)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        yield
        mgr.shutdown()

    app = FastAPI(title="GuardianPay AI", version="3.1", lifespan=lifespan)
    app.state.mgr = mgr
    llm_status = {"checked_at": None, "ok": None, "models": [], "error": None}

    @app.exception_handler(InstanceBusy)
    async def _busy(_r: Request, e: InstanceBusy):
        return JSONResponse(status_code=409, content={"error": "busy", "detail": str(e)})

    # ------------------------------------------------------------------ dependências
    def instance(iid: str) -> Instance:
        try:
            return mgr.get(iid)
        except KeyError:
            raise HTTPException(404, "instância inexistente") from None

    def session(iid: str, authorization: str | None = Header(default=None)) -> tuple[Instance, str, str]:
        inst = instance(iid)
        token = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else None
        with inst.lock:
            try:
                actor = inst.rt.sessions.resolve(token)
            except AuthError as e:
                raise HTTPException(401, str(e)) from None
        return inst, actor, token

    import hmac
    import os
    import secrets
    loopback = {"127.0.0.1", "::1", "localhost"} | set(local_hosts or ())
    # token de operador (administração): GP_ADMIN_TOKEN; no modo demo, gerado por processo e entregue só a clientes locais
    operator_token = os.environ.get("GP_ADMIN_TOKEN") or (secrets.token_urlsafe(24) if s.demo_mode else None)

    def is_local(request: Request) -> bool:
        return bool(request.client and request.client.host in loopback)

    def demo_only(request: Request):
        if not s.demo_mode:
            raise HTTPException(403, "disponível apenas em modo demonstração (GP_DEMO_MODE=1)")
        if not is_local(request):
            raise HTTPException(403, "modo demonstração só aceita clientes locais (loopback)")

    def operator(request: Request, x_gp_operator: str | None = Header(default=None)):
        """Operações administrativas (instâncias, integrações): exigem token de operador; no modo demo, também loopback."""
        if not operator_token:
            raise HTTPException(403, "administração desabilitada: defina GP_ADMIN_TOKEN no servidor")
        if not x_gp_operator or not hmac.compare_digest(x_gp_operator, operator_token):
            raise HTTPException(401, "token de operador ausente ou inválido")
        if s.demo_mode and not is_local(request):
            raise HTTPException(403, "operador de demonstração só a partir do host local")

    @app.get("/api/demo/operator")
    def demo_operator(request: Request):
        """Mecanismo LOCAL e explicitamente de demonstração: entrega o token de operador do processo a clientes loopback."""
        if not s.demo_mode or not is_local(request):
            raise HTTPException(404)
        return {"token": operator_token, "aviso": "token de operador LOCAL de demonstração (gerado neste processo; não use em produção)"}

    def actor_info(inst: Instance, actor_id: str) -> dict:
        a = inst.rt.actors.get(actor_id)
        return {"login": a.actor_id, "nome": a.nome, "papel": a.role, "pessoa_id": a.pessoa_id,
                "alcada_cents": a.alcada_cents, "demo": s.demo_mode}

    # ------------------------------------------------------------------ sistema
    @app.get("/api/health")
    def health():
        return {"ok": True, "demo_mode": s.demo_mode}

    @app.get("/api/integrations/llm", dependencies=[Depends(operator)])
    def llm_get():
        return {"configurado": bool(s.ai_proxy_api_key), "modo": s.llm_mode, "base_url": s.ai_proxy_base_url,
                "modelo_configurado": (s.ai_proxy_model or "").strip() or None,
                "ultima_verificacao": llm_status, "observacao": "estado real; nenhuma resposta simulada é apresentada como inferência"}

    @app.post("/api/integrations/llm/check", dependencies=[Depends(operator)])
    def llm_check():
        """Verificação REAL, sem retry. Com AI_PROXY_MODEL: UMA geração mínima com esse modelo (não lista /v1/models).
        Sem AI_PROXY_MODEL: GET /v1/models (não consome tokens). Erros vêm classificados e sem credencial."""
        if not s.llm_enabled:
            llm_status.update(checked_at=time.time(), ok=False, models=[], error="desligado (sem chave ou GP_LLM_MODE=off)", tipo="LLMDisabled")
            return llm_get()
        cli = ProxyClient(s, None, timeout=20, max_retries=0, breaker_threshold=1, max_calls=1)
        if (s.ai_proxy_model or "").strip():
            p = cli.probe()
            llm_status.update(checked_at=time.time(), ok=p["ok"], models=[p["modelo_servido"]] if p["ok"] else [], error=p["erro"],
                              tipo=p["tipo"], etapa="geracao", uso=p["uso"])
            return llm_get()
        try:
            llm_status.update(checked_at=time.time(), ok=True, models=cli.list_models(), error=None, tipo=None, etapa="listagem")
        except Exception as e:  # noqa: BLE001
            llm_status.update(checked_at=time.time(), ok=False, models=[], error=str(e)[:400], tipo=type(e).__name__, etapa="listagem")
        return llm_get()

    # ------------------------------------------------------------------ instâncias
    @app.get("/api/instances", dependencies=[Depends(operator)])
    def list_instances():
        return [{"id": i.iid, "nome": i.name, "sim_now": i.rt.clock.now.isoformat(), "tick": i.rt.clock.tick,
                 "executando": i.running, "simulador_aprovacao": i.approval_sim} for i in mgr.instances.values()]

    @app.delete("/api/instances/{iid}", dependencies=[Depends(operator)])
    def delete_instance(iid: str):
        instance(iid)
        mgr.delete(iid)
        return {"ok": True, "removida": iid}

    @app.post("/api/instances", status_code=201, dependencies=[Depends(operator)])
    def create_instance(body: CreateInstance):
        if body.approval_simulator and not s.demo_mode:
            raise HTTPException(403, "simulador de aprovação só em modo demonstração")
        inst = mgr.create(body.name, llm_mode=body.llm_mode, approval_sim=body.approval_simulator)
        return {"id": inst.iid, "nome": inst.name}

    # ------------------------------------------------------------------ autenticação
    @app.get("/api/instances/{iid}/auth/demo-users")
    def demo_users(iid: str, _=Depends(demo_only), __=Depends(operator)):
        inst = instance(iid)
        return [actor_info(inst, a.actor_id) for a in inst.rt.actors._actors.values()  # noqa: SLF001
                if a.kind == "human" and a.active]

    @app.post("/api/instances/{iid}/auth/demo-login")
    def demo_login(iid: str, body: DemoLogin, _=Depends(demo_only), __=Depends(operator)):
        inst = instance(iid)
        with inst.lock:
            try:
                token = inst.rt.sessions.issue_demo(body.login, purpose="web")
            except (AuthError, LookupError) as e:
                raise HTTPException(403, str(e)) from None
        return {"token": token, "usuario": actor_info(inst, body.login), "aviso": "sessão de DEMONSTRAÇÃO (login simulado)"}

    @app.post("/api/instances/{iid}/auth/sso")
    def sso_login(iid: str, body: SSOLogin):
        raise HTTPException(501, "provedor de identidade não configurado nesta entrega (use SessionManager.issue_from_identity_provider)")

    @app.get("/api/instances/{iid}/me")
    def me(ctx=Depends(session)):
        inst, actor, _ = ctx
        return actor_info(inst, actor)

    @app.post("/api/instances/{iid}/auth/logout")
    def logout(ctx=Depends(session)):
        inst, _, token = ctx
        with inst.lock:
            inst.rt.sessions.revoke(token)
        return {"ok": True}

    # ------------------------------------------------------------------ leitura
    @app.get("/api/instances/{iid}/version")
    def version(ctx=Depends(session)):
        inst = ctx[0]
        c = inst.rt.conn
        with inst.lock:
            return {"tick": inst.rt.clock.tick, "sim_now": inst.rt.clock.now.isoformat(), "executando": inst.running,
                    "audit": c.execute("SELECT coalesce(max(seq),0) FROM audit_log").fetchone()[0],
                    "transicoes": c.execute("SELECT coalesce(max(seq),0) FROM case_transitions").fetchone()[0],
                    "chat": c.execute("SELECT coalesce(max(seq),0) FROM chat_messages").fetchone()[0],
                    "erro": inst.last_error}

    @app.get("/api/instances/{iid}/overview")
    def overview(ctx=Depends(session)):
        inst = ctx[0]
        rt, c = inst.rt, inst.rt.conn
        with inst.lock:
            sb = compute(rt)
            lotes = _rows(c, "SELECT l.lote_remessa, l.banco_pagador, l.data_prevista, l.horario_fechamento, l.status, "
                             "(SELECT count(*) FROM op_titulos o WHERE o.lote_remessa=l.lote_remessa AND o.status='agendado') AS titulos, "
                             "(SELECT coalesce(sum(valor_cents),0) FROM op_titulos o WHERE o.lote_remessa=l.lote_remessa AND o.status='agendado') AS valor_cents, "
                             "(SELECT count(*) FROM op_titulos o WHERE o.lote_remessa=l.lote_remessa AND o.bloqueado=1) AS bloqueados, "
                             "lc.titulos_enviados, lc.valor_enviado_cents FROM op_lotes l LEFT JOIN lote_closures lc USING(lote_remessa) "
                             "WHERE l.data_prevista>='2026-11-03' ORDER BY l.data_prevista, l.horario_fechamento")
            timeline = _rows(c, "SELECT * FROM (SELECT t.sim_ts, 'caso' AS tipo, t.case_id AS ref, t.from_state || ' → ' || t.to_state AS titulo, "
                                "t.reason AS detalhe, t.actor AS ator, t.seq AS ord FROM case_transitions t "
                                "UNION ALL SELECT a.sim_ts, 'acao', coalesce(a.target,''), a.action, a.decision, a.actor, a.seq + 1000000 FROM audit_log a "
                                "WHERE a.decision IN ('executed','requires_approval','denied','failed')) ORDER BY sim_ts DESC, ord DESC LIMIT 60")
            ok_chain, _ = verify_chain(c)
            return {
                "agente": {"executando": inst.running, "intervalo_s": inst.interval_s, "sim_now": rt.clock.now.isoformat(),
                           "tick": rt.clock.tick, "fim": rt.clock.finished, "ultimo_ciclo": inst.last_cycle, "erro": inst.last_error,
                           "llm": "habilitado" if (rt.llm and rt.llm.available) else ("desligado" if rt.llm is None else "indisponível"),
                           "llm_motivo": rt.llm.unavailable_reason() if rt.llm is not None else "desligado por configuração",
                           "simulador_aprovacao": inst.approval_sim, "auditoria_integra": ok_chain},
                "casos_por_estado": dict(c.execute("SELECT state, count(*) FROM cases GROUP BY state").fetchall()),
                "casos_por_severidade": dict(c.execute("SELECT severity, count(*) FROM cases WHERE state<>'resolved' GROUP BY severity").fetchall()),
                "aprovacoes_pendentes": c.execute("SELECT count(*) FROM approval_requests WHERE status='pending'").fetchone()[0],
                "acoes_executadas": c.execute("SELECT count(*) FROM audit_log WHERE decision='executed'").fetchone()[0],
                "lotes": lotes,
                "indicadores": {k: v for k, v in sb.items() if not k.startswith("detalhe")},
                "timeline": timeline,
                "proximo_corte": _rows(c, "SELECT lote_remessa, data_prevista || 'T' || horario_fechamento || ':00' AS fecha_em, "
                                          "(SELECT count(*) FROM op_titulos o WHERE o.lote_remessa=l.lote_remessa AND o.status='agendado' AND o.bloqueado=0) AS sairao, "
                                          "(SELECT count(*) FROM op_titulos o WHERE o.lote_remessa=l.lote_remessa AND o.bloqueado=1) AS retidos "
                                          "FROM op_lotes l WHERE status='aberto' ORDER BY data_prevista, horario_fechamento LIMIT 1"),
                "ferramentas": dict(c.execute("SELECT action, count(*) FROM audit_log WHERE decision='executed' AND actor LIKE 'agent:%' "
                                              "GROUP BY action ORDER BY 2 DESC").fetchall()),
            }

    # ------------------------------------------------------------------ diagnóstico do LLM (sem credencial)
    def _llm_diag(inst) -> dict:
        rt, c = inst.rt, inst.rt.conn
        if rt.llm is None:
            st = rt.settings
            return {"instancia": inst.iid, "estado_cliente": "desligado",
                    "motivo": "GP_LLM_MODE=off" if st.llm_mode == "off" else "AI_PROXY_API_KEY ausente (reinicie após configurar o .env)",
                    "modelo_configurado": (st.ai_proxy_model or "").strip() or None}
        d = rt.llm.diagnostics() | {"instancia": inst.iid}
        cols = {r[1] for r in c.execute("PRAGMA table_info(llm_usage)")}
        if "channel" in cols:
            d["ultimas_chamadas"] = _rows(c, "SELECT seq, sim_ts, model, status, channel, purpose, input_tokens, output_tokens, attempts, "
                                             "error_type, http_status, latency_ms, finish_reason FROM llm_usage WHERE endpoint='chat' "
                                             "ORDER BY seq DESC LIMIT 10")
        if c.execute("SELECT name FROM sqlite_master WHERE name='llm_breaker_log'").fetchone():
            d["transicoes_disjuntor"] = _rows(c, "SELECT seq, at, from_state, to_state, cause, kind, http_status, actor "
                                                 "FROM llm_breaker_log ORDER BY seq DESC LIMIT 10")
        return d

    @app.get("/api/instances/{iid}/llm/diagnostico")
    def llm_diagnostico(ctx=Depends(session)):
        inst = ctx[0]
        with inst.lock:
            return _llm_diag(inst)

    @app.post("/api/instances/{iid}/llm/disjuntor/recuperar", dependencies=[Depends(operator)])
    def llm_recover(iid: str):
        """Recuperação CONTROLADA: OPEN -> HALF_OPEN (a próxima chamada é o teste). Não zera orçamento nem histórico."""
        inst = instance(iid)
        with inst.lock:
            if inst.rt.llm is None:
                raise HTTPException(409, "LLM desligado nesta instância")
            inst.rt.llm.breaker.recover("operador")
            return _llm_diag(inst)

    @app.get("/api/instances/{iid}/demo/journey")
    def demo_journey(ctx=Depends(session)):
        from .journey import journey
        inst = ctx[0]
        with inst.lock:
            return journey(inst.rt)

    @app.get("/api/instances/{iid}/cases")
    def cases(ctx=Depends(session), state: str | None = None):
        inst = ctx[0]
        with inst.lock:
            rows = _rows(inst.rt.conn, "SELECT case_id, rule_id, state, severity, certainty, exposure_cents, priority, titulos, updated_sim_ts, body "
                                       "FROM cases" + (" WHERE state=?" if state else "") + " ORDER BY priority DESC, case_id", (state,) if state else ())
        for r in rows:
            b = json.loads(r.pop("body"))
            r.update(titulos=json.loads(r["titulos"])[:5], n_titulos=len(json.loads(r["titulos"])), strength=b["strength"],
                     summary=b["summary"], fornecedor_id=b["fornecedor_id"], lote_remessa=b["lote_remessa"])
        return rows

    @app.get("/api/instances/{iid}/cases/{cid}")
    def case_detail(cid: str, ctx=Depends(session)):
        inst = ctx[0]
        c = inst.rt.conn
        with inst.lock:
            row = c.execute("SELECT * FROM cases WHERE case_id=?", (cid,)).fetchone()
            if row is None:
                raise HTTPException(404, "caso inexistente")
            body = json.loads(row["body"])
            tids = body["titulos"][:60]
            q = ",".join("?" * len(tids)) or "''"
            pend = json.loads(row["pending_request"] or "{}")
            sol_ids = [p["solicitacao_id"] for p in pend.get("approvals", [])]
            sol_rows = _rows(c, "SELECT * FROM approval_requests WHERE case_id=? OR solicitacao_id IN (%s) ORDER BY created_sim_ts"
                             % (",".join("?" * len(sol_ids)) or "''"), (cid, *sol_ids))
            sol_ids = sorted({*sol_ids, *(r["solicitacao_id"] for r in sol_rows)})
            ev_by_source: dict[str, list] = {}
            for e in body["evidences"]:
                ev_by_source.setdefault(e["source"], []).append(e)
            return {
                "caso": {k: row[k] for k in ("case_id", "rule_id", "state", "severity", "certainty", "exposure_cents", "priority",
                                             "attempts", "last_error", "created_sim_ts", "updated_sim_ts")},
                "achado": body, "evidencias_por_fonte": ev_by_source,
                "titulos": _rows(c, f"SELECT o.*, n.categoria, n.nf_num FROM op_titulos o JOIN n_titulos n USING(titulo_id) WHERE titulo_id IN ({q})", tids),
                "transicoes": _rows(c, "SELECT * FROM case_transitions WHERE case_id=? ORDER BY seq", (cid,)),
                "decisoes": [d | {"actions": json.loads(d["actions"]), "policy_refs": json.loads(d["policy_refs"])}
                             for d in _rows(c, "SELECT * FROM case_decisions WHERE case_id=? ORDER BY seq", (cid,))],
                "acoes": _rows(c, "SELECT seq, sim_ts, actor, actor_role, action, target, decision, idempotency_key FROM audit_log "
                                  "WHERE idempotency_key LIKE ? OR target IN (%s) ORDER BY seq" % (",".join("?" * len(sol_ids)) or "''"),
                               (f"{cid}:%", *sol_ids)),
                "aprovacoes": [r | {"required": json.loads(r["required"]), "approvals": json.loads(r["approvals"]),
                                    "routed_to": json.loads(r["routed_to"] or "[]"), "params": json.loads(r["params"])} for r in sol_rows],
                "ia": _rows(c, "SELECT * FROM ai_traces WHERE case_id=? ORDER BY seq", (cid,))
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='ai_traces'").fetchone() else [],
                "pendencia": pend.get("target"),
            }

    # ------------------------------------------------------------------ aprovações
    @app.get("/api/instances/{iid}/approvals")
    def approvals(ctx=Depends(session), status: str = "pending"):
        inst, actor, _ = ctx
        c = inst.rt.conn
        out = []
        with inst.lock:
            for r in _rows(c, "SELECT * FROM approval_requests WHERE status=? ORDER BY created_sim_ts DESC", (status,)):
                params = json.loads(r["params"])
                d = inst.rt.engine.evaluate(ToolRequest(tool="aprovar_acao", actor_id=actor, params={"solicitacao_id": r["solicitacao_id"]}),
                                            inst.rt.clock.now.isoformat())
                case = {"case_id": r["case_id"]} if r.get("case_id") else None
                out.append({**r, "params": {k: v for k, v in params.items() if not k.startswith("_")}, "valor_cents": params.get("_amount_cents", 0),
                            "required": json.loads(r["required"]), "approvals": json.loads(r["approvals"]),
                            "routed_to": json.loads(r["routed_to"] or "[]"), "evidence": json.loads(r["evidence"]),
                            "case_id": case["case_id"] if case else None,
                            "posso_aprovar": d.outcome.value == "allow" and status == "pending", "motivo_bloqueio": d.reasons,
                            "simuladas": [dict(x) for x in c.execute("SELECT * FROM simulated_decisions WHERE solicitacao_id=?", (r["solicitacao_id"],))]})
        return out

    def _decide(ctx, sid: str, tool: str, params: dict, key: str | None):
        inst, actor, token = ctx

        def run():
            res = execute_with_session(inst.rt, token, tool, params, idempotency_key=key)
            resumed = None
            if res.status.value == "ok":
                resumed = inst.rt.orchestrator.cycle()   # retomada automática do agente após a decisão (sem avançar relógio)
            case = inst.rt.conn.execute("SELECT c.case_id, c.state FROM approval_requests a JOIN cases c ON c.case_id=a.case_id "
                                        "WHERE a.solicitacao_id=?", (sid,)).fetchone()
            return {"resultado": res.model_dump(), "agente_retomado": resumed, "caso": dict(case) if case else None}
        return inst.command(key, actor, tool, run)

    @app.post("/api/instances/{iid}/approvals/{sid}/approve")
    def approve(sid: str, body: Decide, ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        return _decide(ctx, sid, "aprovar_acao", {"solicitacao_id": sid, "comentario": body.comentario}, idempotency_key)

    @app.post("/api/instances/{iid}/approvals/{sid}/reject")
    def reject(sid: str, body: Reject, ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        return _decide(ctx, sid, "recusar_acao", {"solicitacao_id": sid, "motivo": body.motivo}, idempotency_key)

    # ------------------------------------------------------------------ agente (conversa)
    @app.get("/api/instances/{iid}/agent/messages")
    def chat_history(ctx=Depends(session), after: int = 0):
        inst = ctx[0]
        with inst.lock:
            return [r | {"payload": json.loads(r["payload"])} for r in _rows(inst.rt.conn, "SELECT * FROM chat_messages WHERE seq>? ORDER BY seq", (after,))]

    @app.post("/api/instances/{iid}/agent/chat")
    def chat(body: ChatIn, ctx=Depends(session)):
        inst, actor, _ = ctx
        if not inst.lock.acquire(timeout=10):
            raise InstanceBusy("instância ocupada")
        try:
            return AgentChat(inst.rt).handle(actor, body.message)
        finally:
            inst.lock.release()

    # ------------------------------------------------------------------ notificações (outbox interno)
    @app.get("/api/instances/{iid}/notifications")
    def notifications(ctx=Depends(session), mine: bool = False):
        inst, actor, _ = ctx
        a = inst.rt.actors.get(actor)
        with inst.lock:
            rows = _rows(inst.rt.conn, "SELECT o.*, (SELECT 1 FROM notification_reads r WHERE r.msg_id=o.msg_id AND r.actor_id=?) AS lida "
                                       "FROM outbox o ORDER BY sim_ts DESC, msg_id LIMIT 200", (actor,))
        if mine:
            rows = [r for r in rows if r["to_ref"] in (actor, a.pessoa_id)]
        return [r | {"titulos": json.loads(r["titulos"]), "simulado": True} for r in rows]

    @app.post("/api/instances/{iid}/notifications/{mid}/read")
    def mark_read(mid: str, ctx=Depends(session)):
        inst, actor, _ = ctx
        with inst.lock:
            inst.rt.conn.execute("INSERT OR IGNORE INTO notification_reads VALUES(?,?)", (mid, actor))
        return {"ok": True}

    # ------------------------------------------------------------------ simulação
    def _sim(ctx, key, kind, fn):
        inst, actor, _ = ctx
        if not key:
            raise HTTPException(400, "cabeçalho Idempotency-Key obrigatório para comandos de simulação")
        return inst.command(key, actor, kind, fn)

    @app.post("/api/instances/{iid}/sim/step", dependencies=[Depends(demo_only)])
    def sim_step(ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        return _sim(ctx, idempotency_key, "step", lambda: ctx[0].step())

    @app.post("/api/instances/{iid}/sim/advance", dependencies=[Depends(demo_only)])
    def sim_advance(body: Advance, ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        inst = ctx[0]

        def run():
            done = 0
            for _ in range(body.hours):
                if inst.step()["executado"]:
                    done += 1
            return {"passos": done, "sim_now": inst.rt.clock.now.isoformat(), "tick": inst.rt.clock.tick}
        return _sim(ctx, idempotency_key, "advance", run)

    @app.post("/api/instances/{iid}/sim/pause", dependencies=[Depends(demo_only)])
    def sim_pause(ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        def run():
            ctx[0].running = False
            return {"executando": False}
        return _sim(ctx, idempotency_key, "pause", run)

    @app.post("/api/instances/{iid}/sim/resume", dependencies=[Depends(demo_only)])
    def sim_resume(body: Resume, ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        def run():
            ctx[0].interval_s, ctx[0].running = body.interval_s, True
            return {"executando": True, "intervalo_s": body.interval_s}
        return _sim(ctx, idempotency_key, "resume", run)

    @app.post("/api/instances/{iid}/sim/approval-simulator", dependencies=[Depends(demo_only)])
    def sim_approver(body: Toggle, ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        def run():
            ctx[0].set_approval_sim(body.enabled)
            return {"simulador_aprovacao": body.enabled, "aviso": "decisões programadas de TESTE; não são manifestações humanas reais"}
        return _sim(ctx, idempotency_key, "approval-sim", run)

    @app.post("/api/instances/{iid}/sim/reset", dependencies=[Depends(demo_only)])
    def sim_reset(ctx=Depends(session), idempotency_key: str | None = Header(default=None)):
        if not idempotency_key:
            raise HTTPException(400, "cabeçalho Idempotency-Key obrigatório")
        inst = mgr.reset(ctx[0].iid)
        return {"id": inst.iid, "sim_now": inst.rt.clock.now.isoformat(), "aviso": "estado reiniciado; sessões anteriores invalidadas"}

    @app.get("/api/instances/{iid}/events")
    def events_list(ctx=Depends(session), after: int = 0, limit: int = 100):
        inst = ctx[0]
        with inst.lock:
            return _rows(inst.rt.conn, "SELECT seq, event_id, sim_ts, type, source, entity_id, trusted, processed, attempts, payload "
                                       "FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, min(int(limit), 500)))

    @app.get("/api/compare")
    def compare(a: str, b: str, authorization: str | None = Header(default=None), x_compare_token: str | None = Header(default=None)):
        """Sessões são por instância: exige token válido em A (Authorization) e em B (X-Compare-Token)."""
        out = {}
        for iid, auth in ((a, authorization), (b, f"Bearer {x_compare_token}" if x_compare_token else None)):
            session(iid, auth)
            inst = instance(iid)
            with inst.lock:
                sb = compute(inst.rt)
                out[iid] = {"nome": inst.name, "sim_now": inst.rt.clock.now.isoformat(), "simulador_aprovacao": inst.approval_sim,
                            "casos": dict(inst.rt.conn.execute("SELECT state, count(*) FROM cases GROUP BY state").fetchall()),
                            "indicadores": {k: v for k, v in sb.items() if not k.startswith("detalhe")}}
        return out

    # ------------------------------------------------------------------ frontend estático
    dist = Path(ROOT) / "web" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = dist / path
            return FileResponse(f if f.is_file() else dist / "index.html")

    app.state.fmt = cents_to_str
    return app


def main() -> None:
    import os

    import uvicorn
    s = load_settings()
    host = os.environ.get("GP_HOST", "127.0.0.1")
    if s.demo_mode and host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("Recusado: modo demonstração (login simulado) não pode ser exposto fora do host local. "
                         "Use GP_HOST=127.0.0.1 ou desligue GP_DEMO_MODE.")
    uvicorn.run(create_app(s), host=host, port=int(os.environ.get("GP_PORT", "8000")))


if __name__ == "__main__":
    main()
