"""Предохранители ордер-движка на живых деньгах.

Заявка обещает деньги ровно так же, как покупка их тратит: площадка
резервирует сумму сразу, до всякого исполнения. Значит, и запреты должны
действовать те же. Иначе включённый ордер-движок становится обходной
дорогой мимо выключенного автомата — самой дорогой ошибкой в проекте.

Проверяется здесь именно исполнение плана, а не его составление: что
именно движок хочет поставить, проверяет test_orders.
"""

from __future__ import annotations

import pytest
from app.config import MarketplaceConfig, Settings
from app.domain import Market
from app.trading import orders as orders_mod
from app.trading.engine import OrderReport, TradingEngine


class FakeAdapter:
    """Площадка, которая только запоминает, о чём её попросили."""

    def __init__(self) -> None:
        self.created: list[tuple[str, float, int]] = []
        self.cancelled: list[str] = []

    async def create_order(self, collection: str, price_ton: float, amount: int) -> None:
        self.created.append((collection, price_ton, amount))

    async def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def make_settings(*, paper: bool, auto: bool, whitelist: list[str] | None = None) -> Settings:
    settings = Settings(paper_mode=paper, auto_trade=auto)
    settings.marketplaces = {"mrkt": MarketplaceConfig(enabled=True, trade_enabled=True)}
    settings.risk.collection_whitelist = ["Evil Eye"] if whitelist is None else whitelist
    settings.orders.enabled = True
    return settings


def plan_of(*intents: orders_mod.OrderIntent) -> orders_mod.OrderPlan:
    return orders_mod.OrderPlan(intents=list(intents))


def place(collection: str = "Evil Eye", price: float = 9.0, amount: int = 1):
    return orders_mod.OrderIntent(
        action=orders_mod.OrderAction.PLACE,
        collection=collection,
        price_ton=price,
        amount=amount,
        reason="тест",
    )


async def apply(engine: TradingEngine, *intents) -> tuple[FakeAdapter, OrderReport]:
    adapter = FakeAdapter()
    report = OrderReport()
    await engine._apply_orders(adapter, plan_of(*intents), report, Market.MRKT)
    return adapter, report


# --- Тумблер автомата -----------------------------------------------------


async def test_live_orders_need_the_auto_trade_switch():
    """Главный запрет: без автомата живых заявок не бывает.

    Ордер-движок и автомат — два пути к одному и тому же: подарок за
    реальные деньги. Разрешить один при выключенном другом значит
    отдать деньги тумблеру, который пользователь считал выключенным.
    """
    engine = TradingEngine(make_settings(paper=False, auto=False))

    report = await engine.run_orders()

    assert report.placed == 0
    assert "автомат" in report.skipped


async def test_paper_mode_needs_no_switch():
    """В симуляции запрещать нечего: деньги не двигаются."""
    engine = TradingEngine(make_settings(paper=True, auto=False))
    adapter, report = await apply(engine, place())

    assert adapter.created == []
    assert report.simulated == 1


# --- Белый список и лимиты ------------------------------------------------


async def test_collection_outside_whitelist_is_not_ordered():
    engine = TradingEngine(make_settings(paper=False, auto=True))
    adapter, report = await apply(engine, place(collection="Plush Pepe"))

    assert adapter.created == []
    assert "whitelist" in report.skipped["Plush Pepe"]


async def test_empty_whitelist_blocks_everything():
    """Пустой список означает «не торгуй ничем», а не «торгуй всем»."""
    engine = TradingEngine(make_settings(paper=False, auto=True, whitelist=[]))
    adapter, _ = await apply(engine, place())

    assert adapter.created == []


async def test_blacklisted_collection_is_not_ordered():
    settings = make_settings(paper=False, auto=True)
    settings.risk.collection_blacklist = ["Evil Eye"]
    engine = TradingEngine(settings)
    adapter, report = await apply(engine, place())

    assert adapter.created == []
    assert "blacklist" in report.skipped["Evil Eye"]


async def test_order_above_the_position_limit_is_skipped():
    settings = make_settings(paper=False, auto=True)
    settings.risk.max_position_ton = 5.0
    engine = TradingEngine(settings)
    adapter, report = await apply(engine, place(price=9.0))

    assert adapter.created == []
    assert "лимита на позицию" in report.skipped["Evil Eye"]


async def test_tripped_breaker_stops_new_orders():
    engine = TradingEngine(make_settings(paper=False, auto=True))
    engine.risk.state.tripped = True
    engine.risk.state.trip_reason = "тест"
    adapter, report = await apply(engine, place())

    assert adapter.created == []
    assert "предохранитель" in report.skipped["Evil Eye"]


async def test_allowed_order_reaches_the_market():
    """Обратная проверка: разрешённое должно проходить, иначе тест пуст."""
    engine = TradingEngine(make_settings(paper=False, auto=True))
    adapter, report = await apply(engine, place())

    assert adapter.created == [("Evil Eye", 9.0, 1)]
    assert report.placed == 1


# --- Деньги ---------------------------------------------------------------


async def test_orders_stop_when_the_balance_runs_out():
    """Заявки резервируют деньги, поэтому план не должен обещать больше,
    чем есть на балансе: площадка отвергнет остаток, но уже своими ошибками.
    """
    from app.trading.wallet import MarketBalance, _now

    settings = make_settings(paper=False, auto=True)
    settings.risk.collection_whitelist = ["Evil Eye", "Cookie Heart"]
    engine = TradingEngine(settings)
    engine.wallet.balances["mrkt"] = MarketBalance(ton=10.0, at=_now())

    adapter, report = await apply(
        engine, place(price=9.0), place(collection="Cookie Heart", price=9.0)
    )

    assert len(adapter.created) == 1, "вторая заявка не обеспечена деньгами"
    assert "баланс" in report.skipped["Cookie Heart"]


async def test_unknown_balance_does_not_block():
    """Незнание баланса — не причина молча остановить торговлю."""
    engine = TradingEngine(make_settings(paper=False, auto=True))
    adapter, _ = await apply(engine, place())

    assert len(adapter.created) == 1


# --- Снятие ---------------------------------------------------------------


async def test_cancel_is_never_blocked():
    """Снятие освобождает деньги. Запирать капитал предохранителем нельзя."""
    engine = TradingEngine(make_settings(paper=False, auto=True, whitelist=[]))
    engine.risk.state.tripped = True
    engine.risk.state.trip_reason = "тест"

    adapter, report = await apply(
        engine,
        orders_mod.OrderIntent(
            action=orders_mod.OrderAction.CANCEL,
            collection="Evil Eye",
            order_id="O-1",
            reason="вышла из диапазона",
        ),
    )

    assert adapter.cancelled == ["O-1"]
    assert report.cancelled == 1


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Ни один тест здесь не должен ходить в сеть."""

    async def refuse(*_args, **_kwargs):
        raise AssertionError("тест не должен обращаться к площадке")

    monkeypatch.setattr(TradingEngine, "_run_orders_on", refuse)
