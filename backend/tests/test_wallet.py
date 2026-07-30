"""Проверки учёта баланса площадки.

Подарки покупаются с внутреннего депозита MRKT, а не с TON-кошелька.
До появления этого учёта баланс нигде не проверялся и в интерфейсе был
прочерком: бот пытался бы купить лот при пустом счёте и получал отказ
площадки вместо понятного «не хватает денег».
"""

from __future__ import annotations

import pytest
from app.config import MarketplaceConfig, Settings
from app.trading import wallet as wallet_mod
from app.trading.wallet import BalanceTracker, MarketBalance


@pytest.fixture
def live_settings() -> Settings:
    settings = Settings(paper_mode=False)
    settings.marketplaces = {
        "mrkt": MarketplaceConfig(enabled=True, trade_enabled=True),
        "portals": MarketplaceConfig(enabled=True, trade_enabled=False),
    }
    return settings


def tracker(settings: Settings, ton: float, *, age_sec: float = 0.0) -> BalanceTracker:
    now = wallet_mod._now()
    box = BalanceTracker(settings)
    box.balances["mrkt"] = MarketBalance(ton=ton, at=now - age_sec)
    return box


def test_sufficient_balance_does_not_block(live_settings):
    assert tracker(live_settings, 50.0).shortfall("mrkt", 12.0) == ""


def test_insufficient_balance_explains_the_gap(live_settings):
    reason = tracker(live_settings, 5.0).shortfall("mrkt", 12.0)
    assert "5.00" in reason and "12.00" in reason


def test_unknown_balance_does_not_block(live_settings):
    """Из-за неудачи одного запроса торговля не должна вставать.

    Отказ площадки всё равно придёт на самой покупке, но уже с причиной.
    """
    box = BalanceTracker(live_settings)
    assert box.available("mrkt") is None
    assert box.shortfall("mrkt", 12.0) == ""


def test_stale_measurement_counts_as_unknown(live_settings):
    box = tracker(live_settings, 50.0, age_sec=wallet_mod.STALE_SEC + 1)
    assert box.available("mrkt") is None


def test_purchases_between_measurements_are_subtracted(live_settings):
    """Быстрая петля может купить несколько лотов между замерами."""
    box = tracker(live_settings, 20.0)
    box.reserve("mrkt", 12.0)

    assert box.available("mrkt") == pytest.approx(8.0)
    assert box.shortfall("mrkt", 12.0) != "", "второй такой лот уже не по карману"


def test_reserve_never_goes_negative(live_settings):
    box = tracker(live_settings, 10.0)
    box.reserve("mrkt", 25.0)
    assert box.available("mrkt") == 0.0


def test_total_is_none_when_nothing_measured(live_settings):
    assert BalanceTracker(live_settings).total_ton is None


def test_total_sums_measured_markets(live_settings):
    box = tracker(live_settings, 20.0)
    box.reserve("mrkt", 5.0)
    assert box.total_ton == pytest.approx(15.0)


async def test_paper_mode_does_not_touch_the_marketplace():
    """В симуляции деньги не двигаются — запрос баланса оставил бы след."""
    settings = Settings(paper_mode=True)
    box = tracker(settings, 20.0)

    assert await box.refresh() == {}
    assert box.total_ton is None, "старый замер должен быть забыт"


async def test_read_only_markets_are_not_asked_for_balance(live_settings, monkeypatch):
    asked: list[str] = []

    def fake_build(market, settings):
        asked.append(market.value)
        raise RuntimeError("до сети дойти не должно")

    monkeypatch.setattr(wallet_mod.registry, "build_adapter", fake_build)

    box = BalanceTracker(live_settings)
    with pytest.raises(RuntimeError):
        await box.refresh()

    assert asked == ["mrkt"], "Portals только читаем — баланс там не нужен"
