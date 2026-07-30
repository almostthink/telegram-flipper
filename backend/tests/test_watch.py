"""Проверки быстрой петли.

Плановый цикл ходит раз в двадцать минут. Недооценённый лот столько не
живёт, поэтому реакция вынесена в отдельную петлю: один запрос к ленте,
пересчёт только затронутых коллекций, покупка под теми же лимитами.

Главное требование к ней — не завести обходной путь мимо проверок. Все
запреты основного цикла обязаны действовать и здесь.
"""

from __future__ import annotations

import pytest
from app.analytics import liquidity as liquidity_mod
from app.analytics import pipeline
from app.analytics.pricing import FairValue
from app.analytics.signals import Signal
from app.config import MarketplaceConfig, Settings
from app.ingest.scanner import ScanStats
from app.storage.db import init_db
from app.trading.engine import TradingEngine

from tests.factories import make_listing


@pytest.fixture
async def db():
    await init_db()
    yield


def make_settings(*, auto: bool = False, paper: bool = True) -> Settings:
    settings = Settings(paper_mode=paper, auto_trade=auto)
    settings.marketplaces = {
        "mrkt": MarketplaceConfig(enabled=True, trade_enabled=True),
        "portals": MarketplaceConfig(enabled=True, trade_enabled=False),
    }
    settings.risk.collection_whitelist = ["Plush Pepe"]
    return settings


def make_signal(listing_id: str, *, passed: bool = True, score: float = 0.8) -> Signal:
    listing = make_listing(listing_id=listing_id, price_ton=8.0)
    listing.market = "mrkt"
    return Signal(
        listing=listing,
        fair=FairValue(value_ton=12.0, conservative_ton=11.0, confidence=0.8, method="test"),
        liquidity=liquidity_mod.LiquidityMetrics(collection="Plush Pepe", score=0.7),
        net_roi=0.3,
        score=score,
        passed=passed,
        explanation="тест",
    )


def wire(engine: TradingEngine, monkeypatch, *, fresh, signals):
    """Подменяем сеть и оценку: петля проверяется, а не площадка."""

    async def fake_scan():
        stats = ScanStats()
        stats.new_listings = sum(len(ids) for ids in fresh.values())
        return stats, fresh

    async def fake_evaluate(_settings, collection):
        return signals

    monkeypatch.setattr(engine.scanner, "scan_new_listings", fake_scan)
    monkeypatch.setattr(pipeline, "evaluate_one", fake_evaluate)


# --- Отбор ---------------------------------------------------------------


async def test_only_fresh_listings_are_considered(db, monkeypatch):
    """Лоты, уже разобранные плановым циклом, второй раз не оцениваются."""
    engine = TradingEngine(make_settings())
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["NEW"]},
        signals=[make_signal("NEW"), make_signal("OLD"), make_signal("ANOTHER-OLD")],
    )

    report = await engine.run_watch()

    assert report.evaluated == 1
    assert report.passed == 1


async def test_nothing_new_means_no_evaluation(db, monkeypatch):
    engine = TradingEngine(make_settings())
    called = False

    async def fake_evaluate(_settings, collection):
        nonlocal called
        called = True
        return []

    async def fake_scan():
        return ScanStats(), {}

    monkeypatch.setattr(engine.scanner, "scan_new_listings", fake_scan)
    monkeypatch.setattr(pipeline, "evaluate_one", fake_evaluate)

    report = await engine.run_watch()

    assert report.evaluated == 0
    assert called is False, "лишний пересчёт — это лишние секунды"


async def test_rejected_signals_are_not_bought(db, monkeypatch):
    engine = TradingEngine(make_settings(auto=True, paper=True))
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["NEW"]},
        signals=[make_signal("NEW", passed=False)],
    )

    report = await engine.run_watch()

    assert report.passed == 0
    assert report.bought == 0


# --- Покупка -------------------------------------------------------------


async def test_auto_trade_off_means_look_but_do_not_buy(db, monkeypatch):
    """Тумблер автомата обязан действовать и на быструю петлю."""
    engine = TradingEngine(make_settings(auto=False))
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["NEW"]},
        signals=[make_signal("NEW")],
    )

    report = await engine.run_watch()

    assert report.passed == 1
    assert report.bought == 0


async def test_passing_signal_is_bought_when_auto_on(db, monkeypatch):
    engine = TradingEngine(make_settings(auto=True, paper=True))
    await engine.risk.refresh(paper=True)
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["NEW"]},
        signals=[make_signal("NEW")],
    )

    report = await engine.run_watch()

    assert report.bought == 1


async def test_best_signal_goes_first(db, monkeypatch):
    """Денег на всех не хватит — покупать надо с лучшего."""
    engine = TradingEngine(make_settings(auto=True, paper=True))
    await engine.risk.refresh(paper=True)
    order: list[str] = []

    original = engine.attempt_buy

    async def spy(signal, **kwargs):
        order.append(signal.listing.listing_id)
        return await original(signal, **kwargs)

    monkeypatch.setattr(engine, "attempt_buy", spy)
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["A", "B"]},
        signals=[make_signal("A", score=0.4), make_signal("B", score=0.9)],
    )

    await engine.run_watch()

    assert order == ["B", "A"]


async def test_tripped_breaker_stops_the_fast_path(db, monkeypatch):
    """Предохранитель не должен обходиться коротким путём."""
    engine = TradingEngine(make_settings(auto=True, paper=True))
    engine.risk.trip("проверка")
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["NEW"]},
        signals=[make_signal("NEW")],
    )

    report = await engine.run_watch()

    assert report.bought == 0
    assert any("предохранитель" in reason for reason in report.blocked)


async def test_insufficient_balance_blocks_the_purchase(db, monkeypatch):
    """Живой режим: лот дороже, чем есть на балансе площадки."""
    from app.trading.wallet import MarketBalance, _now

    engine = TradingEngine(make_settings(auto=True, paper=False))
    await engine.risk.refresh(paper=False)
    engine.wallet.balances["mrkt"] = MarketBalance(ton=1.0, at=_now())

    bought: list[str] = []

    async def fake_buy(listing, **kwargs):
        bought.append(listing.listing_id)
        raise AssertionError("до площадки доходить не должно")

    monkeypatch.setattr(engine.executor, "buy", fake_buy)
    wire(
        engine,
        monkeypatch,
        fresh={"Plush Pepe": ["NEW"]},
        signals=[make_signal("NEW")],
    )

    report = await engine.run_watch()

    assert bought == []
    assert report.bought == 0
    assert any("баланс" in reason for reason in report.blocked)


# --- Устойчивость --------------------------------------------------------


async def test_overlapping_tick_is_skipped(db, monkeypatch):
    """Тик длиннее периода — очередь копить незачем, рынок уже ушёл."""
    engine = TradingEngine(make_settings())
    engine.watching = True

    called = False

    async def fake_scan():
        nonlocal called
        called = True
        return ScanStats(), {}

    monkeypatch.setattr(engine.scanner, "scan_new_listings", fake_scan)

    await engine.run_watch()

    assert called is False


async def test_failed_collection_does_not_drop_the_tick(db, monkeypatch):
    """Одна сломанная коллекция не должна отменять остальные."""
    engine = TradingEngine(make_settings())

    async def fake_scan():
        stats = ScanStats()
        stats.new_listings = 2
        return stats, {"Broken": ["X"], "Plush Pepe": ["NEW"]}

    async def fake_evaluate(_settings, collection):
        if collection == "Broken":
            raise RuntimeError("развалилось")
        return [make_signal("NEW")]

    monkeypatch.setattr(engine.scanner, "scan_new_listings", fake_scan)
    monkeypatch.setattr(pipeline, "evaluate_one", fake_evaluate)

    report = await engine.run_watch()

    assert report.evaluated == 1
    assert any("Broken" in error for error in report.errors)


async def test_watching_flag_is_released_after_failure(db, monkeypatch):
    """Иначе одна ошибка глушит петлю навсегда."""
    engine = TradingEngine(make_settings())

    async def boom():
        raise RuntimeError("сеть отвалилась")

    monkeypatch.setattr(engine.scanner, "scan_new_listings", boom)

    report = await engine.run_watch()

    assert engine.watching is False
    assert report.errors
