"""Composição do runtime (sem framework): settings -> banco -> ingestão -> políticas/catálogo -> executor.

O núcleo não conhece interface: API, CLI, Slack/Telegram etc. são adaptadores que chamam Runtime.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import events
from .clock import BankCalendar, SimClock
from .config import Settings, load_settings
from .identity import ActorRegistry
from .ingest import ingest, load_calendar
from .policies import PolicySet, load_policies
from .policy_engine import PolicyEngine
from .store import connect, get_state, set_state, transaction
from .tools.catalog import ToolSpec, load_catalog
from .tools.executor import ToolExecutor
from .tools.handlers import default_handlers


@dataclass
class Runtime:
    settings: Settings
    conn: object
    clock: SimClock
    calendar: BankCalendar
    policies: PolicySet
    catalog: dict[str, ToolSpec]
    actors: ActorRegistry
    engine: PolicyEngine
    executor: ToolExecutor
    sessions: object = None
    orchestrator: object = None
    llm: object = None

    def advance(self, n: int = 1) -> str:
        """Avança o relógio persistindo o tique (retomada segura após reinício). Não executa o agente."""
        with transaction(self.conn):
            for _ in range(n):
                self.clock.advance(1)
                ts = self.clock.now.isoformat()
                set_state(self.conn, "sim_tick", str(self.clock.tick))
                events.emit(self.conn, type_=events.EventType.tique, source="clock", sim_ts=ts,
                            payload={"tick": self.clock.tick}, trusted=True)
        return self.clock.now.isoformat()

    def cycle(self) -> dict:
        """Um ciclo no instante atual: 1) fecha lotes cujo corte chegou; 2) agente processa eventos e casos."""
        from .sim.scheduler import close_due_lots
        closed = close_due_lots(self)
        sim = getattr(self, "approval_simulator", None)
        simulated = sim.tick() if sim is not None else []   # SIMULAÇÃO DE TESTE (decisões programadas)
        stats = self.orchestrator.cycle()
        if simulated:
            stats["decisoes_simuladas"] = len(simulated)
        return {"sim_now": self.clock.now.isoformat(), "lotes_fechados": closed, **stats}

    def step(self) -> dict | None:
        """Um passo: executa o ciclo do instante atual se ainda não feito; senão avança 1 h e executa. None = fim."""
        if get_state(self.conn, f"cycle_done_{self.clock.tick}") == "1":
            if self.clock.finished:
                return None
            self.advance(1)
        out = self.cycle()
        with transaction(self.conn):
            set_state(self.conn, f"cycle_done_{self.clock.tick}", "1")
        return out

    def run(self, ticks: int | None = None) -> list[dict]:
        """Executa o ciclo no instante atual e avança tique a tique (independe de interface aberta)."""
        out = []
        if get_state(self.conn, f"cycle_done_{self.clock.tick}") != "1":
            out.append(self.cycle())
            with transaction(self.conn):
                set_state(self.conn, f"cycle_done_{self.clock.tick}", "1")
        remaining = self.clock.ticks_total - self.clock.tick if ticks is None else min(ticks, self.clock.ticks_total - self.clock.tick)
        for _ in range(remaining):
            self.advance(1)
            out.append(self.cycle())
            with transaction(self.conn):
                set_state(self.conn, f"cycle_done_{self.clock.tick}", "1")
        return out


def bootstrap(settings: Settings | None = None, *, reingest: bool = False) -> Runtime:
    s = settings or load_settings()
    if reingest:  # reingestão = estado novo e coerente (op_*, auditoria e casos zerados juntos)
        for suffix in ("", "-wal", "-shm"):
            p = s.db_path.with_name(s.db_path.name + suffix)
            if p.exists():
                p.unlink()
    conn = connect(s.db_path)
    if reingest or get_state(conn, "ingested") != "1":
        ingest(conn, s.raw_dir, s.sim_start.isoformat())
    clock = SimClock(start=s.sim_start, ticks_total=s.sim_ticks)
    clock.set_tick(int(get_state(conn, "sim_tick", "0")))
    policies = load_policies(s.raw_dir / "politicas.csv")
    catalog = load_catalog(s.raw_dir / "ferramentas.csv")
    actors = ActorRegistry.from_db(conn)
    engine = PolicyEngine(conn, catalog, policies, actors)
    executor = ToolExecutor(conn, catalog, engine, actors, clock, default_handlers())
    rt = Runtime(s, conn, clock, load_calendar(conn), policies, catalog, actors, engine, executor)
    from .agent.orchestrator import Orchestrator
    from .auth import SessionManager
    from .llm.proxy_client import build_client
    rt.sessions = SessionManager(conn, clock, actors, demo_mode=s.demo_mode)
    rt.llm = build_client(s, conn)   # um cliente por instância: disjuntor/orçamento no banco desta instância
    rt.orchestrator = Orchestrator(rt, llm=rt.llm)
    return rt
