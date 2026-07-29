"""Сквозная проверка торгового цикла в paper-режиме.

Проверяет всю цепочку на реальной базе: покупка → выставление →
лестница переоценки → закрытие по рыночной сделке → статистика.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.config import Settings
from app.domain import utcnow
from app.storage.db import init_db, session_scope
from app.storage.models import Position, SaleRecord, TradeLog
from app.trading.executor import Executor

from tests.factories import make_listing


@pytest.fixture
async def db():
    await init_db()
    async with session_scope() as session:
        for model in (TradeLog, Position, SaleRecord):
            for row in list((await session.execute(_select_all(model))).scalars()):
                await session.delete(row)
    yield


def _select_all(model):
    from sqlalchemy import select

    return select(model)


def settings_paper() -> Settings:
    settings = Settings()
    settings.paper_mode = True
    return settings


async def test_buy_creates_position_and_log(db):
    executor = Executor(settings_paper())
    listing = make_listing(listing_id="L1", price_ton=8.0)

    result = await executor.buy(listing, fair_value=12.0, expected_tts=10.0, reason="тест")

    assert result.ok
    async with session_scope() as session:
        position = await session.get(Position, result.position_id)
        assert position.status == "open"
        assert position.buy_price_ton == 8.0
        assert position.fair_value_at_buy == 12.0
        assert position.paper is True
        # Обоснование обязано сохраниться: без него журнал бесполезен.
        assert position.reason == "тест"


async def test_list_and_reprice_flow(db):
    executor = Executor(settings_paper())
    bought = await executor.buy(make_listing(listing_id="L2", price_ton=10.0), fair_value=14.0)

    async with session_scope() as session:
        position = await session.get(Position, bought.position_id)

    listed = await executor.list_for_sale(position, 16.0)
    assert listed.ok

    async with session_scope() as session:
        position = await session.get(Position, bought.position_id)
        assert position.status == "listed"
        assert position.ask_price_ton == 16.0

    repriced = await executor.reprice(position, 15.0, "шаг лестницы")
    assert repriced.ok

    async with session_scope() as session:
        position = await session.get(Position, bought.position_id)
        assert position.ask_price_ton == 15.0
        assert position.reprice_count == 1
        assert position.last_repriced_at is not None


async def test_close_accounts_for_fees_and_gas(db):
    settings = settings_paper()
    settings.marketplaces["portals"].fee_sell = 0.05
    settings.analytics.gas_ton = 0.1
    executor = Executor(settings)

    bought = await executor.buy(make_listing(listing_id="L3", price_ton=10.0))
    result = await executor.close(bought.position_id, 12.0, "продано")
    assert result.ok

    async with session_scope() as session:
        position = await session.get(Position, bought.position_id)

    # 12 × 0.95 − 0.1 = 11.3 выручка; 10 + 0.1 = 10.1 затраты → +1.2
    assert position.net_pnl_ton == pytest.approx(1.2, abs=1e-6)
    assert position.status == "closed"


async def test_close_at_breakeven_markup_yields_zero(db):
    """Наценка ровно в размере безубытка должна дать нулевой результат."""
    settings = settings_paper()
    settings.analytics.gas_ton = 0.0
    executor = Executor(settings)

    from app.analytics.pricing import breakeven_markup

    buy_price = 10.0
    sell_price = buy_price * (1 + breakeven_markup(0.05))

    bought = await executor.buy(make_listing(listing_id="L4", price_ton=buy_price))
    await executor.close(bought.position_id, sell_price, "безубыток")

    async with session_scope() as session:
        position = await session.get(Position, bought.position_id)
    assert position.net_pnl_ton == pytest.approx(0.0, abs=1e-6)


async def test_paper_position_closes_only_on_real_market_sale(db):
    """Симуляция не должна выдумывать продажу: нужна реальная сделка по цене аска."""
    executor = Executor(settings_paper())
    bought = await executor.buy(
        make_listing(listing_id="L5", price_ton=10.0, model="Epic"), fair_value=14.0
    )
    async with session_scope() as session:
        position = await session.get(Position, bought.position_id)
    await executor.list_for_sale(position, 15.0)

    # Рынка нет — позиция остаётся открытой.
    assert await executor.settle_paper_positions() == []

    # Сделка ниже нашего аска тоже не закрывает позицию.
    async with session_scope() as session:
        session.add(
            SaleRecord(
                market="portals",
                external_id="s-low",
                collection="Plush Pepe",
                model="Epic",
                price_ton=12.0,
                sold_at=utcnow(),
            )
        )
    assert await executor.settle_paper_positions() == []

    # А вот сделка по цене аска — закрывает.
    async with session_scope() as session:
        session.add(
            SaleRecord(
                market="portals",
                external_id="s-high",
                collection="Plush Pepe",
                model="Epic",
                price_ton=15.5,
                sold_at=utcnow() + timedelta(seconds=1),
            )
        )
    closed = await executor.settle_paper_positions()
    assert closed == [bought.position_id]


async def test_stats_separate_realized_from_unrealized(db):
    """Бумажная переоценка не должна попадать в реализованную прибыль."""
    from app.analytics import stats as stats_mod

    executor = Executor(settings_paper())

    closed = await executor.buy(make_listing(listing_id="L6", price_ton=10.0))
    await executor.close(closed.position_id, 13.0, "продано")

    open_buy = await executor.buy(make_listing(listing_id="L7", price_ton=10.0))
    async with session_scope() as session:
        position = await session.get(Position, open_buy.position_id)
    await executor.list_for_sale(position, 20.0)

    result = await stats_mod.compute(paper=True, days=30)

    assert result.closed_trades == 1
    assert result.open_positions == 1
    assert result.realized_pnl_ton > 0
    assert result.unrealized_pnl_ton > 0
    # Это разные величины и складывать их нельзя.
    assert result.realized_pnl_ton != result.unrealized_pnl_ton
    assert result.win_rate == 1.0
