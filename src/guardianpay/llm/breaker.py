"""Disjuntor (circuit breaker) do LLM — máquina de estados explícita, por instância, persistida no banco da instância.

CLOSED --(N falhas transitórias seguidas | 1 falha permanente)--> OPEN
OPEN   --(cooldown vencido)--> HALF_OPEN (exatamente UMA chamada de teste em voo)
HALF_OPEN --(sucesso)--> CLOSED          HALF_OPEN --(falha)--> OPEN (novo cooldown)

* Falha TRANSITÓRIA: timeout, rede, 429, 5xx/524 — conta para o limiar.
* Falha PERMANENTE: 401/403/modelo inválido/requisição recusada — abre na hora, com cooldown maior
  (repetir não resolve; o operador corrige a configuração e pede recuperação controlada).
* Saída rejeitada (JSON/contrato inválido, truncamento) NÃO é falha de transporte: o proxy respondeu.
* Estado e histórico de transições ficam no SQLite da instância: reiniciar o servidor não "esconde" um disjuntor
  aberto, e instâncias independentes não compartilham estado.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_breaker(
  id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL, consecutive_failures INTEGER NOT NULL,
  opened_at REAL, cooldown_s REAL, cause TEXT, kind TEXT, http_status INTEGER, updated_at REAL);
CREATE TABLE IF NOT EXISTS llm_breaker_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
  cause TEXT, kind TEXT, http_status INTEGER, actor TEXT NOT NULL);
"""

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"
TRANSIENT, PERMANENT = "transitoria", "permanente"


class CircuitBreaker:
    def __init__(self, conn=None, *, threshold: int = 2, cooldown_s: float = 60.0, permanent_cooldown_s: float = 900.0,
                 clock: Callable[[], float] = time.time):
        self.conn, self.threshold, self.cooldown_s, self.permanent_cooldown_s = conn, max(1, threshold), cooldown_s, permanent_cooldown_s
        self.clock = clock
        self._lock = threading.Lock()
        self.state, self.failures, self.opened_at, self.cur_cooldown = CLOSED, 0, None, cooldown_s
        self.cause, self.kind, self.http_status = None, None, None
        self._trial_in_flight = False
        if conn is not None:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT * FROM llm_breaker WHERE id=1").fetchone()
            if row:   # retomada após reinício: o estado real continua valendo
                self.state, self.failures = row["state"], row["consecutive_failures"]
                self.opened_at, self.cur_cooldown = row["opened_at"], row["cooldown_s"] or cooldown_s
                self.cause, self.kind, self.http_status = row["cause"], row["kind"], row["http_status"]
                if self.state == HALF_OPEN:          # teste interrompido pelo reinício: volta a OPEN, com cooldown já vencido
                    self.state = OPEN

    # ------------------------------------------------------------------ consulta
    def retry_in_s(self) -> float:
        if self.state != OPEN or self.opened_at is None:
            return 0.0
        return max(0.0, self.opened_at + self.cur_cooldown - self.clock())

    def blocked_reason(self) -> str | None:
        """Motivo de bloqueio SEM mudar estado (para diagnóstico/`available`)."""
        with self._lock:
            if self.state == OPEN and self.retry_in_s() > 0:
                return (f"disjuntor OPEN ({self.kind}: {self.cause}); nova tentativa em {self.retry_in_s():.0f}s")
            if self.state == HALF_OPEN and self._trial_in_flight:
                return "disjuntor HALF_OPEN: chamada de teste em andamento"
            return None

    def snapshot(self) -> dict:
        return {"estado": self.state, "falhas_consecutivas": self.failures, "limiar": self.threshold,
                "causa_abertura": self.cause, "tipo_falha": self.kind, "http_status": self.http_status,
                "aberto_em": self.opened_at, "cooldown_s": self.cur_cooldown if self.state != CLOSED else self.cooldown_s,
                "recupera_em_s": round(self.retry_in_s(), 1), "teste_em_andamento": self._trial_in_flight}

    # ------------------------------------------------------------------ transições
    def acquire(self) -> tuple[bool, str | None]:
        """Pede permissão para UMA chamada. OPEN com cooldown vencido => HALF_OPEN com uma única tentativa."""
        with self._lock:
            if self.state == CLOSED:
                return True, None
            if self.state == OPEN:
                if self.retry_in_s() > 0:
                    return False, f"disjuntor OPEN ({self.kind}: {self.cause}); nova tentativa em {self.retry_in_s():.0f}s"
                self._move(HALF_OPEN, "cooldown vencido: chamada de teste", self.kind, self.http_status, "sistema")
            if self._trial_in_flight:
                return False, "disjuntor HALF_OPEN: chamada de teste em andamento"
            self._trial_in_flight = True
            return True, None

    def release(self) -> None:
        """Chamada terminou sem veredito de transporte (ex.: abortada antes do envio)."""
        with self._lock:
            self._trial_in_flight = False

    def on_success(self) -> None:
        with self._lock:
            self._trial_in_flight = False
            self.failures = 0
            if self.state != CLOSED:
                self.cause = self.kind = self.http_status = None
                self.opened_at = None
                self._move(CLOSED, "chamada de teste bem-sucedida", None, None, "sistema")

    def on_failure(self, kind: str, cause: str, http_status: int | None = None) -> None:
        with self._lock:
            self._trial_in_flight = False
            self.failures += 1
            if self.state == HALF_OPEN or kind == PERMANENT or self.failures >= self.threshold:
                self.opened_at = self.clock()
                self.cur_cooldown = self.permanent_cooldown_s if kind == PERMANENT else self.cooldown_s
                self.cause, self.kind, self.http_status = cause[:300], kind, http_status
                self._move(OPEN, cause, kind, http_status, "sistema")
            else:
                self._save()

    def recover(self, actor: str) -> dict:
        """Recuperação CONTROLADA (operador): OPEN -> HALF_OPEN. Não fecha às cegas nem zera orçamento/uso:
        a próxima chamada é o teste; se falhar, o disjuntor reabre com a causa real."""
        with self._lock:
            if self.state == OPEN:
                self.opened_at = self.clock() - self.cur_cooldown
                self._move(HALF_OPEN, "recuperação controlada solicitada", self.kind, self.http_status, actor)
            return self.snapshot()

    # ------------------------------------------------------------------ persistência
    def _move(self, to: str, cause: str | None, kind: str | None, http_status: int | None, actor: str) -> None:
        frm, self.state = self.state, to
        if self.conn is not None:
            self.conn.execute("INSERT INTO llm_breaker_log(at,from_state,to_state,cause,kind,http_status,actor) VALUES(?,?,?,?,?,?,?)",
                              (self.clock(), frm, to, (cause or "")[:300], kind, http_status, actor))
        self._save()

    def _save(self) -> None:
        if self.conn is None:
            return
        self.conn.execute("INSERT INTO llm_breaker(id,state,consecutive_failures,opened_at,cooldown_s,cause,kind,http_status,updated_at) "
                          "VALUES(1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
                          "consecutive_failures=excluded.consecutive_failures, opened_at=excluded.opened_at, cooldown_s=excluded.cooldown_s, "
                          "cause=excluded.cause, kind=excluded.kind, http_status=excluded.http_status, updated_at=excluded.updated_at",
                          (self.state, self.failures, self.opened_at, self.cur_cooldown, self.cause, self.kind, self.http_status, self.clock()))
