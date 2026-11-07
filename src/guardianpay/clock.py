"""Relógio de simulação independente do relógio do computador + calendário bancário."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

TICK = timedelta(hours=1)


class ClockError(RuntimeError):
    pass


@dataclass
class SimClock:
    """Relógio discreto: cada tique = 1 hora corrida. Não lê datetime.now()."""

    start: datetime
    ticks_total: int = 168
    tick: int = 0

    @property
    def now(self) -> datetime:
        return self.start + self.tick * TICK

    @property
    def end(self) -> datetime:
        return self.start + self.ticks_total * TICK

    @property
    def finished(self) -> bool:
        return self.tick >= self.ticks_total

    def advance(self, n: int = 1) -> datetime:
        if n < 0:
            raise ClockError("o relógio de simulação não volta no tempo")
        if self.tick + n > self.ticks_total:
            raise ClockError(f"avanço além do fim da simulação ({self.end.isoformat()})")
        self.tick += n
        return self.now

    def set_tick(self, tick: int) -> None:
        """Usado apenas para retomar estado persistido."""
        if not 0 <= tick <= self.ticks_total:
            raise ClockError("tique fora do período")
        self.tick = tick


class BankCalendar:
    """Dias úteis bancários (sem fim de semana e sem feriados de feriados_bancarios.csv)."""

    def __init__(self, holidays: set[date]):
        self.holidays = holidays

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_business_day(self, d: date, include_today: bool = True) -> date:
        cur = d if include_today else d + timedelta(days=1)
        while not self.is_business_day(cur):
            cur += timedelta(days=1)
        return cur

    def lote_close(self, d: date, horario: str) -> datetime:
        hh, mm = (int(x) for x in horario.split(":"))
        return datetime.combine(d, time(hh, mm))

    def is_team_working(self, ts: datetime) -> bool:
        """Equipe lança títulos só em dia útil, 08h–18h (parametros.md)."""
        return self.is_business_day(ts.date()) and 8 <= ts.hour < 18
