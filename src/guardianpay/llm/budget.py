"""Orçamento de LLM por instância e por tarefa — limites configuráveis e causa específica quando bloqueia.

* Instância: chamadas (tentativas, inclusive falhas) e tokens somados a partir de `llm_usage` do banco da instância
  (reiniciar o servidor NÃO zera o consumo).
* Canal: o agente autônomo (ciclos) usa no máximo `autonomous_share` do orçamento da instância, para que a
  conversa (Agent Workspace) nunca fique sem orçamento por causa do segundo plano.
* Tarefa: chamadas, tokens e prazo total (relógio de parede) por investigação/caso.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

AUTONOMOUS, INTERACTIVE, DIAGNOSTIC = "autonomo", "interativo", "diagnostico"


@dataclass(frozen=True)
class LLMLimits:
    timeout_s: float = 90.0               # < ~125 s (corte das rotas síncronas no gateway)
    connect_retries: int = 1              # só falhas ANTES do envio/429/5xx; timeout de leitura não é reenviado
    max_tokens_per_call: int = 2000
    max_calls_per_instance: int = 60
    max_tokens_per_instance: int = 150_000
    autonomous_share: float = 0.6
    max_calls_per_task: int = 3
    max_tokens_per_task: int = 24_000
    task_deadline_s: float = 110.0
    max_concurrency: int = 2              # por processo (o rate limit do proxy é por token, não por instância)
    breaker_threshold: int = 2           # falhas TRANSITÓRIAS seguidas (cada uma já inclui os reenvios de conexão)
    breaker_cooldown_s: float = 60.0
    breaker_permanent_cooldown_s: float = 900.0
    max_llm_cases_per_cycle: int = 3      # ciclo autônomo: casos com raciocínio LLM por tique
    max_llm_seconds_per_cycle: float = 120.0   # ciclo autônomo: tempo total de LLM por tique (o lock da instância fica preso)

    @classmethod
    def from_env(cls, env) -> "LLMLimits":
        def num(key, default, cast, lo, hi):
            try:
                v = cast(env.get(key, default))
            except (TypeError, ValueError):
                v = default
            return min(max(v, lo), hi)
        d = cls()
        return cls(
            timeout_s=num("GP_LLM_TIMEOUT_S", d.timeout_s, float, 5, 120),
            connect_retries=num("GP_LLM_CONNECT_RETRIES", d.connect_retries, int, 0, 3),
            max_tokens_per_call=num("GP_LLM_MAX_TOKENS_CALL", d.max_tokens_per_call, int, 64, 8000),
            max_calls_per_instance=num("GP_LLM_MAX_CALLS_INSTANCE", d.max_calls_per_instance, int, 1, 1000),
            max_tokens_per_instance=num("GP_LLM_MAX_TOKENS_INSTANCE", d.max_tokens_per_instance, int, 1000, 2_000_000),
            autonomous_share=num("GP_LLM_AUTONOMOUS_SHARE", d.autonomous_share, float, 0.0, 0.9),
            max_calls_per_task=num("GP_LLM_MAX_CALLS_TASK", d.max_calls_per_task, int, 1, 6),
            max_tokens_per_task=num("GP_LLM_MAX_TOKENS_TASK", d.max_tokens_per_task, int, 500, 200_000),
            task_deadline_s=num("GP_LLM_TASK_DEADLINE_S", d.task_deadline_s, float, 10, 600),
            max_concurrency=num("GP_LLM_MAX_CONCURRENCY", d.max_concurrency, int, 1, 8),
            breaker_threshold=num("GP_LLM_BREAKER_THRESHOLD", d.breaker_threshold, int, 1, 10),
            breaker_cooldown_s=num("GP_LLM_BREAKER_COOLDOWN_S", d.breaker_cooldown_s, float, 5, 3600),
            breaker_permanent_cooldown_s=num("GP_LLM_BREAKER_PERMANENT_COOLDOWN_S", d.breaker_permanent_cooldown_s, float, 30, 86_400),
            max_llm_cases_per_cycle=num("GP_LLM_MAX_CASES_PER_CYCLE", d.max_llm_cases_per_cycle, int, 0, 50),
            max_llm_seconds_per_cycle=num("GP_LLM_MAX_SECONDS_PER_CYCLE", d.max_llm_seconds_per_cycle, float, 0, 1800),
        )


class InstanceBudget:
    def __init__(self, limits: LLMLimits, conn=None):
        self.lim, self.conn = limits, conn
        self.calls = {AUTONOMOUS: 0, INTERACTIVE: 0, DIAGNOSTIC: 0}
        self.tokens = {AUTONOMOUS: 0, INTERACTIVE: 0, DIAGNOSTIC: 0}
        if conn is not None:      # consumo persistido: reinício não zera contadores
            for r in conn.execute("SELECT coalesce(channel,'autonomo') AS ch, count(*) AS n, "
                                  "coalesce(sum(coalesce(input_tokens,0)+coalesce(output_tokens,0)),0) AS tok "
                                  "FROM llm_usage WHERE endpoint='chat' GROUP BY ch"):
                ch = r["ch"] if r["ch"] in self.calls else AUTONOMOUS
                self.calls[ch] += r["n"]
                self.tokens[ch] += r["tok"]

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())

    def blocked_reason(self, channel: str) -> str | None:
        L = self.lim
        if self.total_calls >= L.max_calls_per_instance:
            return f"orçamento da instância esgotado: chamadas {self.total_calls}/{L.max_calls_per_instance}"
        if self.total_tokens >= L.max_tokens_per_instance:
            return f"orçamento da instância esgotado: tokens {self.total_tokens}/{L.max_tokens_per_instance}"
        if channel == AUTONOMOUS:
            cap_c, cap_t = int(L.max_calls_per_instance * L.autonomous_share), int(L.max_tokens_per_instance * L.autonomous_share)
            if self.calls[AUTONOMOUS] >= cap_c:
                return (f"cota do agente autônomo esgotada: chamadas {self.calls[AUTONOMOUS]}/{cap_c} "
                        f"(o restante fica reservado à conversa)")
            if self.tokens[AUTONOMOUS] >= cap_t:
                return f"cota do agente autônomo esgotada: tokens {self.tokens[AUTONOMOUS]}/{cap_t}"
        return None

    def charge(self, channel: str, tokens: int = 0) -> None:
        ch = channel if channel in self.calls else AUTONOMOUS
        self.calls[ch] += 1
        self.tokens[ch] += max(0, int(tokens))

    def add_tokens(self, channel: str, tokens: int) -> None:
        ch = channel if channel in self.calls else AUTONOMOUS
        self.tokens[ch] += max(0, int(tokens))

    def snapshot(self) -> dict:
        L = self.lim
        return {"chamadas": dict(self.calls), "tokens": dict(self.tokens),
                "chamadas_restantes": max(0, L.max_calls_per_instance - self.total_calls),
                "tokens_restantes": max(0, L.max_tokens_per_instance - self.total_tokens),
                "limites": {"chamadas_instancia": L.max_calls_per_instance, "tokens_instancia": L.max_tokens_per_instance,
                            "cota_autonomo": L.autonomous_share, "chamadas_tarefa": L.max_calls_per_task,
                            "tokens_tarefa": L.max_tokens_per_task, "tokens_chamada": L.max_tokens_per_call,
                            "prazo_tarefa_s": L.task_deadline_s, "timeout_s": L.timeout_s,
                            "concorrencia": L.max_concurrency, "casos_llm_por_ciclo": L.max_llm_cases_per_cycle,
                            "segundos_llm_por_ciclo": L.max_llm_seconds_per_cycle}}


@dataclass
class TaskBudget:
    """Limites de UMA investigação (caso do ciclo autônomo ou tarefa composta do chat)."""
    max_calls: int
    max_tokens: int
    deadline_s: float
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    tokens: int = 0

    @classmethod
    def from_limits(cls, lim: LLMLimits) -> "TaskBudget":
        return cls(lim.max_calls_per_task, lim.max_tokens_per_task, lim.task_deadline_s)

    def remaining_s(self) -> float:
        return self.deadline_s - (time.monotonic() - self.started)

    def blocked_reason(self) -> str | None:
        if self.calls >= self.max_calls:
            return f"limite de chamadas por tarefa ({self.calls}/{self.max_calls})"
        if self.tokens >= self.max_tokens:
            return f"limite de tokens por tarefa ({self.tokens}/{self.max_tokens})"
        if self.remaining_s() <= 5:
            return f"prazo da tarefa esgotado ({self.deadline_s:.0f}s)"
        return None

    def charge(self, tokens: int) -> None:
        self.calls += 1
        self.tokens += max(0, int(tokens))
