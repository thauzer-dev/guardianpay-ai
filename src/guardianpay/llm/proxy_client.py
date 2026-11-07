"""Cliente do AI Proxy (formato OpenAI) — motor de raciocínio OPCIONAL.

Regras (skill ai-proxy + Entrega 2):
* credencial só via AI_PROXY_API_KEY (.env fora do git); nunca em log, repr ou exceção;
* modelo: AI_PROXY_MODEL explícito é usado direto; sem ele, escolhe entre os listados em GET /v1/models
  (primeira opção de AI_PROXY_MODEL_PREFERENCE disponível);
* deepseek-* bloqueado; timeout; retry limitado (429 respeitando reset, 5xx/timeout com backoff);
  resposta malformada => erro tipado; disjuntor CLOSED/OPEN/HALF_OPEN (llm/breaker.py) e orçamento (llm/budget.py) por instância;
* uso/custo de cada chamada registrado (tokens; US$ só quando o proxy informa);
* o núcleo funciona sem LLM: toda chamada tem fallback determinístico no chamador.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from ..log import get_logger, log
from .breaker import PERMANENT, TRANSIENT, CircuitBreaker
from .budget import AUTONOMOUS, DIAGNOSTIC, INTERACTIVE, InstanceBudget, LLMLimits, TaskBudget
from .jsonout import content_text

LOG = get_logger("llm")

USAGE_SCHEMA = """CREATE TABLE IF NOT EXISTS llm_usage(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, sim_ts TEXT, endpoint TEXT, model TEXT, status TEXT,
  input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, purpose TEXT, attempts INTEGER)"""
USAGE_COLUMNS = {"channel": "TEXT", "error_type": "TEXT", "http_status": "INTEGER", "latency_ms": "INTEGER", "finish_reason": "TEXT"}
_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_SEM_LOCK = threading.Lock()


def _semaphore(base_url: str, n: int) -> threading.BoundedSemaphore:
    """Concorrência por processo e por gateway: o rate limit do proxy é do token, compartilhado pelas instâncias."""
    with _SEM_LOCK:
        return _SEMAPHORES.setdefault((base_url, n), threading.BoundedSemaphore(n))


def channel_of(purpose: str) -> str:
    if purpose.startswith(("investigacao", "ciclo")):
        return AUTONOMOUS
    if purpose.startswith("probe"):
        return DIAGNOSTIC
    return INTERACTIVE

DEFAULT_PREFERENCE = ("gpt-6-luna", "glm-5.3-flash", "mimo-v2.6-flash", "gpt-6-sol", "glm-5.3")
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 524}


class LLMError(RuntimeError):
    """Base: qualquer falha do LLM. O chamador SEMPRE tem fallback determinístico.

    Atributos de diagnóstico (sem credencial): `kind` (transitoria | permanente | saida | bloqueio | config),
    `stage` (antes_da_chamada | durante_chamada | processamento_resposta), `http_status`, `attempts`."""
    kind, stage, http_status, attempts = "transitoria", "durante_chamada", None, 0

    def tag(self, kind: str | None = None, stage: str | None = None, http_status: int | None = None, attempts: int | None = None):
        if kind is not None:
            self.kind = kind
        if stage is not None:
            self.stage = stage
        if http_status is not None:
            self.http_status = http_status
        if attempts is not None:
            self.attempts = attempts
        return self


class LLMDisabled(LLMError):
    kind, stage = "config", "antes_da_chamada"


class LLMUnavailable(LLMError):
    pass


class LLMTimeout(LLMUnavailable):
    """Sem resposta dentro do timeout (a requisição foi enviada; NÃO é reenviada para não duplicar custo)."""


class LLMCircuitOpen(LLMUnavailable):
    kind, stage = "bloqueio", "antes_da_chamada"


class LLMBusy(LLMUnavailable):
    """Concorrência máxima de chamadas atingida (limite por processo/token)."""
    kind, stage = "bloqueio", "antes_da_chamada"


class LLMMalformed(LLMError):
    kind, stage = "saida", "processamento_resposta"


class LLMTruncated(LLMMalformed):
    """finish_reason=length: max_tokens consumido (inclusive por raciocínio) antes de concluir a resposta."""


class ModelBlocked(LLMError, ValueError):
    kind, stage = "config", "antes_da_chamada"


class LLMBudgetExceeded(LLMError):
    """Limite de chamadas/tokens (instância, canal ou tarefa) atingido: o chamador segue no modo determinístico."""
    kind, stage = "bloqueio", "antes_da_chamada"


class LLMAuthError(LLMError):
    """401: credencial ausente, inválida ou expirada (verifique AI_PROXY_API_KEY, sem o prefixo 'Bearer')."""


class LLMForbidden(LLMError):
    """403: o proxy (ou um WAF à frente dele) recusou a requisição."""


class LLMModelListForbidden(LLMForbidden):
    """403/401 em /v1/models sem AI_PROXY_MODEL definido: não há como escolher modelo sem inventar identificador."""


class LLMModelInvalid(LLMError):
    """400/404 apontando o modelo: identificador inexistente ou não liberado para este token."""


USER_AGENT = "GuardianPay/3.2 (+python; ai-proxy client)"
# Segundo o guia do AI Proxy (skill ai-proxy do evento): modelos "extras" raciocinam antes de responder e pedem max_tokens >= 2000
EXTRA_MODELS_PREFIX = ("glm-", "mimo-", "k3", "kimi-")


def _redact(text: str) -> str:
    import re
    return re.sub(r"(gw-tok-|sk-|Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", text)


def _diagnose(status: int, headers: dict, body: bytes, path: str) -> str:
    """Mensagem curta e SEM credencial para erros 4xx: distingue bloqueio de WAF (Cloudflare) de recusa do proxy."""
    try:
        txt = body.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        txt = ""
    snippet = _redact(" ".join(txt.split()))[:200]
    server = (headers.get("server") or "").lower()
    hint = ""
    if status == 403 and ("cloudflare" in server or "error code: 10" in txt.lower() or "cf-ray" in headers):
        hint = (" — bloqueio do WAF/Cloudflare (ex.: 'error code: 1010' = assinatura do cliente HTTP recusada); "
                "o cliente agora envia User-Agent explícito; se persistir, peça liberação à organização do evento")
    elif status == 403:
        hint = (" — o proxy recusou a autorização para este endpoint/token" +
                ("; /v1/models pode não estar liberado: defina AI_PROXY_MODEL para gerar sem listar" if path == "/models" else ""))
    elif status == 401:
        hint = " — credencial inválida/ausente: confira AI_PROXY_API_KEY (valor sem 'Bearer ' e sem aspas)"
    return f"{path}: HTTP {status}{hint}" + (f" | resposta: {snippet}" if snippet else "")


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: bytes


Transport = Callable[[str, str, dict, bytes | None, float], HttpResponse]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> HttpResponse:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HttpResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")
    # URLError/timeout sobem para o retry


@dataclass
class LLMResponse:
    content: str | dict
    model: str
    usage_line: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    attempts: int = 1
    latency_s: float = 0.0


@dataclass
class SessionUsage:
    calls: int = 0
    failed_calls: int = 0
    chat_tokens: int = 0
    usd: float = 0.0
    lines: list[str] = field(default_factory=list)


class ProxyClient:
    """Um cliente por instância (Runtime): disjuntor, orçamento e uso ficam no banco DAQUELA instância."""

    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None, *, timeout: float | None = None,
                 max_retries: int = 2, transport: Transport | None = None, sleeper: Callable[[float], None] = time.sleep,
                 breaker_threshold: int | None = None, preference: tuple[str, ...] | None = None,
                 max_calls: int | None = None, max_total_tokens: int | None = None,
                 breaker_cooldown_s: float | None = None, now_fn: Callable[[], float] = time.time,
                 limits: LLMLimits | None = None):
        lim = limits or LLMLimits()
        over = {k: v for k, v in {"timeout_s": timeout, "breaker_threshold": breaker_threshold, "max_calls_per_instance": max_calls,
                                  "max_tokens_per_instance": max_total_tokens, "breaker_cooldown_s": breaker_cooldown_s}.items()
                if v is not None}
        self.limits = LLMLimits(**{**lim.__dict__, **over}) if over else lim
        self._s, self._conn, self._timeout, self._max_retries = settings, conn, self.limits.timeout_s, max_retries
        self._transport, self._sleep, self._now = transport or urllib_transport, sleeper, now_fn
        self._models: list[str] | None = None
        self._preference = preference or tuple(
            m.strip() for m in (settings.ai_proxy_model_preference or ",".join(DEFAULT_PREFERENCE)).split(",") if m.strip())
        self.usage = SessionUsage()
        if conn is not None:
            conn.execute(USAGE_SCHEMA)
            have = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")}
            for col, typ in USAGE_COLUMNS.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {typ}")
        self.breaker = CircuitBreaker(conn, threshold=self.limits.breaker_threshold, cooldown_s=self.limits.breaker_cooldown_s,
                                      permanent_cooldown_s=self.limits.breaker_permanent_cooldown_s, clock=now_fn)
        self.budget = InstanceBudget(self.limits, conn)
        self._sem = _semaphore(settings.ai_proxy_base_url, self.limits.max_concurrency)
        self.last_call: dict = {"tentou_chamada": False}

    def __repr__(self) -> str:  # nunca expõe credencial
        return f"ProxyClient(base_url={self._s.ai_proxy_base_url!r}, breaker={self.breaker.state})"

    # ------------------------------------------------------------------ estado (diagnóstico)
    @property
    def _breaker_open(self) -> bool:          # compatibilidade
        return self.breaker.state != "CLOSED"

    @property
    def max_calls(self) -> int:
        return self.limits.max_calls_per_instance

    @property
    def max_total_tokens(self) -> int:
        return self.limits.max_tokens_per_instance

    def unavailable_reason(self, channel: str = INTERACTIVE) -> str | None:
        """Causa ESPECÍFICA de indisponibilidade (None = pode chamar). Prefixos: desligado | orcamento | disjuntor."""
        if not self._s.llm_enabled:
            return "desligado: " + ("GP_LLM_MODE=off" if self._s.llm_mode == "off" else "AI_PROXY_API_KEY ausente")
        b = self.budget.blocked_reason(channel)
        if b:
            return "orcamento: " + b
        d = self.breaker.blocked_reason()
        if d:
            return "disjuntor: " + d
        return None

    @property
    def available(self) -> bool:
        return self.unavailable_reason(INTERACTIVE) is None

    @property
    def budget_exhausted(self) -> bool:
        return self.budget.blocked_reason(INTERACTIVE) is not None

    def diagnostics(self) -> dict:
        why = self.unavailable_reason(INTERACTIVE)
        estado = ("pronto" if why is None else "desligado" if why.startswith("desligado") else
                  "bloqueado_orcamento" if why.startswith("orcamento") else "bloqueado_disjuntor")
        return {"estado_cliente": estado, "motivo": why, "motivo_agente_autonomo": self.unavailable_reason(AUTONOMOUS),
                "modelo_configurado": (self._s.ai_proxy_model or "").strip() or None, "modo": self._s.llm_mode,
                "base_url": self._s.ai_proxy_base_url, "endpoint": "/chat/completions",
                "parametros": {"timeout_s": self._timeout, "reenvios_de_conexao": self._max_retries,
                               "max_tokens_por_chamada": self.limits.max_tokens_per_call, "reenvia_timeout": False},
                "disjuntor": self.breaker.snapshot(), "orcamento": self.budget.snapshot(),
                "ultima_chamada": dict(self.last_call),
                "sessao": {"chamadas_ok": self.usage.calls, "falhas": self.usage.failed_calls, "tokens": self.usage.chat_tokens}}

    # ------------------------------------------------------------------ infraestrutura
    @staticmethod
    def _check_model(model: str) -> None:
        if model.lower().startswith("deepseek"):
            raise ModelBlocked("modelos deepseek-* estão fora do evento; use glm-5.3 ou kimi-for-coding")

    def _key(self) -> str:
        """Normaliza erros comuns de .env no Windows (BOM, aspas, espaços, prefixo 'Bearer ') sem expor o valor."""
        k = (self._s.ai_proxy_api_key or "").strip().strip("﻿").strip('"').strip("'").strip()
        return k[7:].strip() if k.lower().startswith("bearer ") else k

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json",
                "Accept": "application/json", "User-Agent": USER_AGENT}

    def _request(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None) -> tuple[dict, dict, int]:
        """UMA requisição lógica = UMA permissão do disjuntor (reenvios internos incluídos).
        Reenvia: falha de conexão antes do envio, 429 (Retry-After), 5xx/524. NÃO reenvia timeout de leitura."""
        if not self._s.llm_enabled:
            raise LLMDisabled("LLM desligado (sem AI_PROXY_API_KEY ou GP_LLM_MODE=off) — modo determinístico")
        ok, why = self.breaker.acquire()
        if not ok:
            raise LLMCircuitOpen(why)
        timeout = min(self._timeout, timeout) if timeout else self._timeout
        url = self._s.ai_proxy_base_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        last, status, attempt = "erro desconhecido", None, 0
        try:
            for attempt in range(1, self._max_retries + 2):
                try:
                    r = self._transport(method, url, self._headers(), data, timeout)
                except urllib.error.URLError as e:          # antes/durante o envio (DNS, conexão, TLS, proxy local)
                    last = f"conexão: {type(getattr(e, 'reason', e)).__name__}"
                    if attempt <= self._max_retries:
                        self._sleep(min(2 ** attempt, 8))
                        continue
                    break
                except (TimeoutError, socket.timeout):        # enviada, sem resposta no prazo: não reenvia (custo duplicado)
                    err = LLMTimeout(f"{path}: sem resposta em {timeout:.0f}s (requisição enviada; não reenviada) — modelos de "
                                     f"raciocínio com contexto longo podem precisar de GP_LLM_TIMEOUT_S maior (máx. 120)")
                    self.breaker.on_failure(TRANSIENT, f"timeout de {timeout:.0f}s em {path}")
                    raise err.tag("transitoria", "durante_chamada", None, attempt)
                except (ConnectionError, OSError) as e:
                    last = f"rede: {type(e).__name__}"
                    if attempt <= self._max_retries:
                        self._sleep(min(2 ** attempt, 8))
                        continue
                    break
                status = r.status
                if r.status == 200:
                    try:
                        payload = json.loads(r.body)
                    except (ValueError, UnicodeDecodeError):
                        # HTTP 200 sem JSON = página de erro do gateway/WAF: falha de TRANSPORTE (conta no disjuntor)
                        self.breaker.on_failure(TRANSIENT, f"{path}: HTTP 200 sem JSON (gateway)", 200)
                        raise LLMMalformed(f"{path}: corpo não é JSON (provável página do gateway)").tag(
                            "transitoria", "processamento_resposta", 200, attempt) from None
                    self.breaker.on_success()
                    return payload, r.headers, attempt
                last = f"HTTP {r.status}"
                if r.status in RETRYABLE_STATUS and attempt <= self._max_retries:
                    wait = 2 ** attempt
                    if r.status == 429:
                        try:
                            wait = float(r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset") or wait)
                        except ValueError:
                            pass
                    self._sleep(min(max(wait, 0.5), 10))
                    continue
                if r.status in (400, 401, 403, 404, 409, 413, 422):
                    msg = _diagnose(r.status, r.headers, r.body, path)
                    body_l = r.body.decode("utf-8", "replace").lower() if r.body else ""
                    if r.status == 401:
                        self.breaker.on_failure(PERMANENT, "HTTP 401 (credencial)", 401)
                        raise LLMAuthError(msg).tag("permanente", "durante_chamada", 401, attempt)
                    if r.status == 403:
                        self.breaker.on_failure(PERMANENT, f"HTTP 403 em {path}", 403)
                        raise (LLMModelListForbidden if path == "/models" else LLMForbidden)(msg).tag("permanente", "durante_chamada", 403, attempt)
                    if r.status in (400, 404, 422) and path == "/chat/completions" and "model" in body_l:
                        self.breaker.on_failure(PERMANENT, f"HTTP {r.status}: modelo recusado", r.status)
                        raise LLMModelInvalid(msg + " — confira AI_PROXY_MODEL contra o guia do AI Proxy").tag(
                            "permanente", "durante_chamada", r.status, attempt)
                    self.breaker.on_failure(TRANSIENT, f"HTTP {r.status} (requisição recusada)", r.status)
                    raise LLMError(msg + " (não reprocessável)").tag("transitoria", "durante_chamada", r.status, attempt)
                break
            self.breaker.on_failure(TRANSIENT, f"{path}: {last}", status)
            raise LLMUnavailable(f"{path}: {last} após {attempt} tentativa(s)").tag("transitoria", "durante_chamada", status, attempt)
        finally:
            self.breaker.release()     # libera teste HALF_OPEN pendente se nada acima deu veredito

    def _record(self, sim_ts, endpoint, model, status, inp, out, reasoning, usd, purpose, attempts, *, channel=None,
                error_type=None, http_status=None, latency_ms=None, finish_reason=None) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT INTO llm_usage(sim_ts,endpoint,model,status,input_tokens,output_tokens,reasoning_tokens,estimated_cost_usd,"
                "purpose,attempts,channel,error_type,http_status,latency_ms,finish_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sim_ts, endpoint, model, status, inp, out, reasoning, usd, purpose, attempts, channel, error_type, http_status,
                 latency_ms, finish_reason))

    # ------------------------------------------------------------------ modelos
    def list_models(self) -> list[str]:
        if self._models is None:
            data, _, _ = self._request("GET", "/models")
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise LLMMalformed("/models sem lista 'data'")
            self._models = [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)
                            and not m["id"].lower().startswith("deepseek")]
        return self._models

    def resolve_model(self) -> str:
        """AI_PROXY_MODEL definido => usado DIRETAMENTE (sem GET /v1/models); a 1ª geração valida o identificador.
        Sem AI_PROXY_MODEL => escolhe entre os listados; se a listagem for proibida, pede configuração explícita."""
        explicit = (self._s.ai_proxy_model or "").strip()
        if explicit:
            self._check_model(explicit)
            return explicit
        try:
            models = self.list_models()
        except (LLMModelListForbidden, LLMAuthError) as e:
            raise LLMModelListForbidden(f"{e} — defina AI_PROXY_MODEL com um identificador do guia do AI Proxy") from None
        for m in self._preference:
            if m in models:
                return m
        raise LLMUnavailable("nenhum modelo da lista de preferência está disponível em /v1/models")

    # ------------------------------------------------------------------ chamadas
    def chat(self, messages: list[dict], *, sim_ts: str = "", purpose: str = "", max_tokens: int | None = None,
             channel: str | None = None, task: TaskBudget | None = None, timeout_s: float | None = None) -> LLMResponse:
        channel = channel or channel_of(purpose)
        self.last_call = {"tentou_chamada": False, "canal": channel, "proposito": purpose, "etapa": "antes_da_chamada"}
        try:
            return self._chat(messages, sim_ts, purpose, max_tokens, channel, task, timeout_s)
        except LLMError as e:
            self.last_call.update(tipo_falha=type(e).__name__, classe=e.kind, etapa=e.stage, http_status=e.http_status,
                                  tentativas=e.attempts, motivo=_redact(str(e))[:300])
            raise

    def _chat(self, messages, sim_ts, purpose, max_tokens, channel, task, timeout_s) -> LLMResponse:
        why = self.budget.blocked_reason(channel) or (task.blocked_reason() if task else None)
        if not self._s.llm_enabled:
            raise LLMDisabled("LLM desligado (sem AI_PROXY_API_KEY ou GP_LLM_MODE=off) — modo determinístico")
        if why:
            raise LLMBudgetExceeded(why)
        model = self.resolve_model()
        self._check_model(model)
        req_tokens = min(max_tokens or self.limits.max_tokens_per_call, self.limits.max_tokens_per_call)
        if model.lower().startswith(EXTRA_MODELS_PREFIX):
            req_tokens = max(req_tokens, 2000)   # raciocínio consome tokens antes do conteúdo (guia do AI Proxy)
        if task is not None:
            timeout_s = min(timeout_s or self._timeout, max(5.0, task.remaining_s()))
        if not self._sem.acquire(timeout=min(30.0, timeout_s or self._timeout)):
            raise LLMBusy(f"concorrência máxima de chamadas ({self.limits.max_concurrency}) atingida")
        prompt_chars = sum(len(content_text(m.get("content"))) for m in messages)
        self.last_call.update(tentou_chamada=True, modelo=model, etapa="durante_chamada", max_tokens=req_tokens,
                              contexto_chars=prompt_chars, timeout_s=min(self._timeout, timeout_s) if timeout_s else self._timeout)
        self.budget.charge(channel)            # tentativa conta no orçamento (inclusive falha)
        t0 = time.monotonic()
        try:
            data, headers, attempts = self._request("POST", "/chat/completions",
                                                    {"model": model, "messages": messages, "max_tokens": req_tokens}, timeout=timeout_s)
        except LLMError as e:
            self.usage.failed_calls += 1
            if task is not None:
                task.charge(0)
            self._record(sim_ts, "chat", model, "error", None, None, None, None, purpose, e.attempts, channel=channel,
                         error_type=type(e).__name__, http_status=e.http_status, latency_ms=int((time.monotonic() - t0) * 1000))
            raise
        finally:
            self._sem.release()
        latency = time.monotonic() - t0
        u = data.get("usage") if isinstance(data, dict) else None
        u = u if isinstance(u, dict) else {}
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        inp, out = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        self.budget.add_tokens(channel, inp + out)
        if task is not None:
            task.charge(inp + out)
        served = headers.get("x-model-served") or (data.get("model") if isinstance(data, dict) else None) or model
        self.last_call.update(etapa="processamento_resposta", tentativas=attempts, tokens_entrada=inp, tokens_saida=out,
                              tokens_raciocinio=reasoning, modelo_servido=served, latencia_s=round(latency, 2))
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            finish = choice.get("finish_reason")
            content = content_text(msg.get("content"))
            if not content.strip():
                if finish == "length":
                    raise LLMTruncated(f"resposta truncada: max_tokens={req_tokens} consumido antes do conteúdo "
                                       f"(raciocínio {reasoning} tok) — aumente GP_LLM_MAX_TOKENS_CALL")
                raise LLMMalformed("choices[0].message.content vazio")
        except LLMMalformed as e:
            self.usage.failed_calls += 1
            self._record(sim_ts, "chat", served, "malformed", inp, out, reasoning, None, purpose, attempts, channel=channel,
                         error_type=type(e).__name__, http_status=200, latency_ms=int(latency * 1000), finish_reason=locals().get("finish"))
            raise e.tag("saida", "processamento_resposta", 200, attempts)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            self.usage.failed_calls += 1
            self._record(sim_ts, "chat", served, "malformed", inp, out, reasoning, None, purpose, attempts, channel=channel,
                         error_type="LLMMalformed", http_status=200, latency_ms=int(latency * 1000))
            raise LLMMalformed(f"chat sem choices[0].message.content ({type(e).__name__}: {e})").tag(
                "saida", "processamento_resposta", 200, attempts) from None
        self.usage.calls += 1
        self.usage.chat_tokens += inp + out
        line = (f"💲 {served} · entrada {inp} tok · saída {out} tok (raciocínio {reasoning}) · sem preço por chamada"
                f" | sessão: {self.usage.calls} chamadas, {self.usage.chat_tokens} tok")
        self.usage.lines.append(line)
        self.last_call.update(ok=True, finish_reason=finish, uso=line)
        self._record(sim_ts, "chat", served, "ok", inp, out, reasoning, None, purpose, attempts, channel=channel,
                     latency_ms=int(latency * 1000), finish_reason=finish)
        log(LOG, "chamada LLM", usage=line, purpose=purpose, channel=channel, latency_s=round(latency, 2), finish=finish)
        return LLMResponse(content, served, line, inp, out, reasoning, finish, attempts, latency)

    def probe(self) -> dict:
        """Verificação mínima: UMA geração curta com o modelo configurado (sem listar modelos).
        Retorna diagnóstico sem credencial: {ok, modelo, modelo_servido, etapa, erro, tipo}."""
        out = {"ok": False, "modelo": (self._s.ai_proxy_model or "").strip() or None, "modelo_servido": None,
               "etapa": "geracao", "erro": None, "tipo": None, "uso": None, "http_status": None, "latencia_s": None}
        try:
            r = self.chat([{"role": "user", "content": "Responda apenas: ok"}], purpose="probe", max_tokens=16)
            out.update(ok=True, modelo_servido=r.model, uso=r.usage_line, latencia_s=round(r.latency_s, 2))
        except LLMError as e:
            out.update(erro=_redact(str(e))[:400], tipo=type(e).__name__, http_status=e.http_status, etapa=e.stage)
        return out

    def decide(self, state, questions: dict, *, sim_ts: str = "", purpose: str = "") -> LLMResponse:
        """Jev (/v1/systemone): triagem tipada. Nunca calcula valores nem autoriza ações."""
        try:
            data, headers, attempts = self._request("POST", "/systemone", {"state": state, "questions": questions})
            answers = data["answers"]
            if not isinstance(answers, dict) or not set(questions) <= set(answers):
                raise KeyError("answers incompleto")
        except (KeyError, TypeError) as e:
            self._record(sim_ts, "systemone", "jev", "malformed", None, None, None, None, purpose, None)
            raise LLMMalformed(f"systemone malformado ({e})") from None
        except LLMError:
            self._record(sim_ts, "systemone", "jev", "error", None, None, None, None, purpose, None)
            raise
        u = data.get("usage") or {}
        try:
            usd = float(u.get("estimated_cost_usd") or headers.get("x-estimated-cost-usd") or 0)
        except ValueError:
            usd = 0.0
        self.usage.calls += 1
        self.usage.usd += usd
        line = (f"💲 {data.get('model', 'jev')} · entrada {u.get('input_tokens', 0)} tok · US$ {usd:.7f}"
                f" | sessão: US$ {self.usage.usd:.7f}")
        self.usage.lines.append(line)
        self._record(sim_ts, "systemone", data.get("model", "jev"), "ok", u.get("input_tokens"), u.get("output_tokens"),
                     0, usd, purpose, attempts)
        log(LOG, "chamada Jev", usage=line, purpose=purpose)
        return LLMResponse(answers, data.get("model", "jev"), line)


def build_client(settings: Settings, conn=None) -> ProxyClient | None:
    """Cliente do runtime de uma instância, com limites lidos do ambiente (.env)."""
    if not settings.llm_enabled:
        return None
    lim = LLMLimits.from_env(os.environ)
    return ProxyClient(settings, conn, max_retries=lim.connect_retries, limits=lim)
