from datetime import date, datetime

import pytest

from guardianpay.clock import ClockError, SimClock
from guardianpay.config import load_settings
from guardianpay.runtime import bootstrap


def test_clock_bounds_and_independence():
    c = SimClock(start=datetime(2026, 11, 2, 8), ticks_total=168)
    assert c.now == datetime(2026, 11, 2, 8)
    c.advance(167)
    assert c.now == datetime(2026, 11, 9, 7)
    c.advance()
    assert c.finished and c.now == datetime(2026, 11, 9, 8)
    with pytest.raises(ClockError):
        c.advance()
    with pytest.raises(ClockError):
        c.advance(-1)


def test_calendar(rt):
    cal = rt.calendar
    assert not cal.is_business_day(date(2026, 11, 2))          # Finados
    assert cal.next_business_day(date(2026, 11, 2)) == date(2026, 11, 3)
    assert cal.next_business_day(date(2026, 11, 7)) == date(2026, 11, 9)
    assert not cal.is_team_working(datetime(2026, 11, 3, 19))
    assert cal.is_team_working(datetime(2026, 11, 3, 9))


def test_clock_resumes_after_restart(rt):
    rt.advance(5)
    rt2 = bootstrap(load_settings(db_path=rt.settings.db_path, raw_dir=rt.settings.raw_dir))
    assert rt2.clock.tick == 5 and rt2.clock.now == datetime(2026, 11, 2, 13)


def test_policies_loaded_from_csv(rt):
    p = rt.policies
    assert p.analista_limite_cents == 500_000 and p.aprovador_limite_cents == 5_000_000
    assert p.custo_acionamento_desnecessario_cents == 6000
    assert p.custo_relacionamento_cents("produto") == 300_000
    assert set(p.pessoas()) == {f"PS{i:02d}" for i in range(1, 12)}
    assert p.bancos_pagadores()["Banco Meridiano"]["fechamento_do_lote"] == "10:00"
    assert p.dispensas() == {"oc": {"utilidades"}, "nf": {"aluguel"}}


def test_catalog(rt):
    cat = rt.catalog
    assert len(cat) == 27  # linhas de ferramentas.csv
    assert cat["corrigir_lancamento"].params_required == ("titulo_id", "campo", "valor_novo", "evidencias")
    assert cat["aprovar_acao"].roles == {"aprovador"}
    assert cat["agendar_titulo"].limits["sem_aprovacao_ate"] == 5000.0
    assert not cat["consultar_titulo"].kind.is_mutation and cat["retirar_do_lote"].kind.is_mutation


def test_agents_cannot_hold_approver_role(rt):
    from guardianpay.identity import Actor, ActorRegistry
    with pytest.raises(ValueError):
        ActorRegistry({"agent:x": Actor("agent:x", "aprovador", "agent")})
    assert rt.actors.get("agent:orchestrator").role == "analista_cp"
    assert rt.actors.get("usuario_02").active is False      # desligado em 30/09
