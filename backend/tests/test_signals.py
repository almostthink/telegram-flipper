"""Проверки генерации сигналов — итоговая логика отбора сделок."""

from __future__ import annotations

from datetime import timedelta

from app.analytics import liquidity as liquidity_mod
from app.analytics.pricing import PricingContext
from app.analytics.signals import EvaluationInput, Reject, evaluate_collection
from app.config import AnalyticsConfig
from app.domain import utcnow

from tests.factories import make_listing, sales_population

#: Флор модели Epic, согласованный с генерирующей моделью в factories:
#: 10 × exp(0.45 × 4.605) ≈ 79 TON.
EPIC_FLOOR = 70.0


def build(
    *,
    ask: float,
    liquid: bool = True,
    floor_trend: float | None = None,
    cross_market_floor: float | None = None,
    max_position: float | None = None,
) -> EvaluationInput:
    # Выборка с разбросом редкости: без него регрессии не на чем учить
    # зависимость цены от редкости, и оценка становится экстраполяцией.
    sales = sales_population(60) if liquid else []
    for index, sale in enumerate(sales):
        sale.gift_external_id = f"g{index}"
        sale.tts_hours = 6.0
    listings = [make_listing(listing_id="target", price_ton=ask, model="Epic", model_rarity=10.0)]

    metrics = liquidity_mod.compute(
        "Plush Pepe", sales=sales, listings=listings, floor_ton=10.0
    )
    if floor_trend is not None:
        metrics.floor_trend_24h = floor_trend

    return EvaluationInput(
        collection="Plush Pepe",
        listings=listings,
        context=PricingContext(
            collection="Plush Pepe",
            floor_ton=10.0,
            attribute_floors={("model", "Epic"): EPIC_FLOOR},
            sales=sales,
        ),
        liquidity=metrics,
        fee_sell_by_market={"portals": 0.05},
        fee_buy_by_market={"portals": 0.0},
        cross_market_floor=cross_market_floor,
        max_position_ton=max_position,
    )


CONFIG = AnalyticsConfig()


def test_clear_bargain_passes():
    signals = evaluate_collection(build(ask=12.0), CONFIG)
    assert len(signals) == 1
    assert signals[0].passed, signals[0].explanation
    assert signals[0].net_roi > CONFIG.min_roi


def test_fairly_priced_lot_rejected_on_roi():
    """Лот по справедливой цене прибыли не даёт — комиссия съедает всё."""
    signals = evaluate_collection(build(ask=EPIC_FLOOR), CONFIG)
    assert signals[0].reject_reason is Reject.LOW_ROI


def test_illiquid_collection_rejected_regardless_of_discount():
    """Даже огромная скидка не спасает, если продать некому."""
    signals = evaluate_collection(build(ask=1.0, liquid=False), CONFIG)
    assert signals[0].reject_reason in (Reject.LOW_LIQUIDITY, Reject.SLOW_TTS)
    assert not signals[0].passed


def test_falling_floor_blocks_buying():
    signals = evaluate_collection(build(ask=12.0, floor_trend=-0.30), CONFIG)
    assert signals[0].reject_reason is Reject.FALLING_FLOOR


def test_cheap_everywhere_is_not_a_bargain():
    """Если лот так же дёшев на референсных площадках — упал весь рынок."""
    signals = evaluate_collection(build(ask=12.0, cross_market_floor=12.0), CONFIG)
    assert signals[0].reject_reason is Reject.CROSS_MARKET


def test_position_limit_respected():
    signals = evaluate_collection(build(ask=12.0, max_position=5.0), CONFIG)
    assert signals[0].reject_reason is Reject.TOO_EXPENSIVE


def test_score_combines_roi_liquidity_and_confidence():
    signal = evaluate_collection(build(ask=12.0), CONFIG)[0]
    expected = max(signal.net_roi, 0) * signal.liquidity.score * signal.fair.confidence
    assert abs(signal.score - expected) < 1e-4


def test_explanation_mentions_breakeven():
    """Обоснование должно называть порог безубытка — 5% комиссии съедают 5.3%."""
    signal = evaluate_collection(build(ask=12.0), CONFIG)[0]
    assert "безубыток" in signal.explanation
    assert "5.3%" in signal.explanation


def test_signals_sorted_by_score():
    data = build(ask=12.0)
    data.listings = [
        make_listing(
            listing_id="cheap", price_ton=11.0, model="Epic", model_rarity=10.0, row_id=1
        ),
        make_listing(
            listing_id="mid", price_ton=20.0, model="Epic", model_rarity=10.0, row_id=2
        ),
        make_listing(
            listing_id="pricey", price_ton=30.0, model="Epic", model_rarity=10.0, row_id=3
        ),
    ]
    signals = evaluate_collection(data, CONFIG)
    scores = [signal.score for signal in signals]
    assert scores == sorted(scores, reverse=True)


def test_locked_lot_is_rejected():
    """Заблокированный для перепродажи лот — отказ независимо от скидки.

    Площадка блокирует свежепереданные подарки на несколько дней. Купить
    такой значит заморозить капитал, а модель ликвидности об этом не знает.
    """
    data = build(ask=12.0)
    data.listings[0].locked = True

    signals = evaluate_collection(data, CONFIG)
    assert signals[0].reject_reason is Reject.RESALE_LOCKED


def test_future_resale_date_is_rejected():
    data = build(ask=12.0)
    data.listings[0].resale_available_at = utcnow() + timedelta(days=3)

    signals = evaluate_collection(data, CONFIG)
    assert signals[0].reject_reason is Reject.RESALE_LOCKED


def test_past_resale_date_is_fine():
    """Дата разблокировки в прошлом ничему не мешает."""
    data = build(ask=12.0)
    data.listings[0].resale_available_at = utcnow() - timedelta(days=30)

    signals = evaluate_collection(data, CONFIG)
    assert signals[0].passed, signals[0].explanation
