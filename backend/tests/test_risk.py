"""Проверки риск-лимитов и предохранителей.

Самая дорогая ошибка проекта — автомат, который торгует без ограничений.
Поэтому здесь проверяется каждый лимит по отдельности.
"""

from __future__ import annotations

from app.config import RiskLimits
from app.trading.risk import RiskManager


def manager(**overrides) -> RiskManager:
    limits = RiskLimits(
        daily_budget_ton=30.0,
        max_position_ton=10.0,
        max_open_positions=3,
        max_positions_per_collection=2,
        circuit_breaker_errors=3,
        circuit_breaker_daily_loss_ton=5.0,
        **overrides,
    )
    return RiskManager(limits)


def test_allows_normal_purchase():
    assert manager(collection_whitelist=["Pepe"]).can_buy("Pepe", 5.0, auto=True)


def test_rejects_position_above_limit():
    decision = manager().can_buy("Pepe", 50.0, auto=False)
    assert not decision
    assert "лимита на позицию" in decision.reason


def test_rejects_when_daily_budget_spent():
    risk = manager()
    risk.state.spent_today_ton = 28.0
    decision = risk.can_buy("Pepe", 5.0, auto=False)
    assert not decision
    assert "бюджет" in decision.reason


def test_rejects_when_too_many_positions():
    risk = manager()
    risk.state.open_positions = 3
    assert not risk.can_buy("Pepe", 1.0, auto=False)


def test_rejects_concentration_in_one_collection():
    risk = manager()
    risk.state.per_collection = {"Pepe": 2}
    decision = risk.can_buy("Pepe", 1.0, auto=False)
    assert not decision
    assert "концентрации" in decision.reason


def test_auto_mode_requires_whitelist():
    """Пустой whitelist означает запрет торговли, а не разрешение всего."""
    risk = manager()
    decision = risk.can_buy("Pepe", 1.0, auto=True)
    assert not decision
    assert "whitelist" in decision.reason

    # Вручную человек может купить что угодно в пределах остальных лимитов.
    assert risk.can_buy("Pepe", 1.0, auto=False)


def test_auto_mode_respects_whitelist_contents():
    risk = manager(collection_whitelist=["Pepe"])
    assert risk.can_buy("Pepe", 1.0, auto=True)
    assert not risk.can_buy("Другая", 1.0, auto=True)


def test_blacklist_blocks_even_manual():
    risk = manager(collection_blacklist=["Scam"])
    assert not risk.can_buy("Scam", 1.0, auto=False)


def test_circuit_breaker_trips_on_consecutive_errors():
    risk = manager()
    for _ in range(3):
        risk.register_error()

    assert risk.state.tripped
    assert not risk.can_buy("Pepe", 1.0, auto=False)


def test_success_resets_error_streak():
    risk = manager()
    risk.register_error()
    risk.register_error()
    risk.register_success()
    risk.register_error()

    assert not risk.state.tripped


def test_circuit_breaker_trips_on_daily_loss():
    risk = manager()
    risk.register_close("Pepe", -6.0)
    assert risk.state.tripped
    assert "убыток" in risk.state.trip_reason


def test_manual_reset_clears_breaker():
    risk = manager()
    risk.trip("тест")
    risk.reset()

    assert not risk.state.tripped
    assert risk.can_buy("Pepe", 1.0, auto=False)


def test_register_buy_updates_counters():
    risk = manager()
    risk.register_buy("Pepe", 4.0)

    assert risk.state.spent_today_ton == 4.0
    assert risk.state.open_positions == 1
    assert risk.state.per_collection["Pepe"] == 1
