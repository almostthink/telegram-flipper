"""Проверки оценки ликвидности."""

from __future__ import annotations

from app.analytics import liquidity

from tests.factories import make_listing, make_sale


def test_dead_collection_scores_low():
    """Ни продаж, ни истории — балл должен быть низким."""
    metrics = liquidity.compute("Dead", sales=[], listings=[], floor_ton=10.0)

    assert metrics.score < 0.3
    assert metrics.sales_7d == 0
    # Прогноз продажи должен быть заведомо неприемлемым.
    assert metrics.expected_tts_hours > 24 * 7


def test_active_collection_scores_high():
    sales = [
        make_sale(sale_id=i, hours_ago=i * 2, tts_hours=6.0)
        for i in range(1, 40)
    ]
    listings = [make_listing(listing_id=f"l{i}", price_ton=11.0) for i in range(3)]

    metrics = liquidity.compute("Alive", sales=sales, listings=listings, floor_ton=10.0)

    assert metrics.score > 0.5
    assert metrics.sales_24h > 0
    assert metrics.tts_median_hours == 6.0


def test_thick_book_reduces_score():
    """Много лотов у флора — очередь на продажу длиннее, балл ниже."""
    sales = [make_sale(sale_id=i, hours_ago=i, tts_hours=8.0) for i in range(1, 30)]

    thin = liquidity.compute(
        "C",
        sales=sales,
        listings=[make_listing(listing_id=f"a{i}", price_ton=10.5) for i in range(2)],
        floor_ton=10.0,
    )
    thick = liquidity.compute(
        "C",
        sales=sales,
        listings=[make_listing(listing_id=f"b{i}", price_ton=10.5) for i in range(80)],
        floor_ton=10.0,
    )

    assert thick.depth_10pct > thin.depth_10pct
    assert thick.score < thin.score


def test_suspicious_sales_are_ignored():
    """Отбракованные антифродом сделки не должны надувать ликвидность."""
    clean = [make_sale(sale_id=i, hours_ago=1) for i in range(1, 5)]
    dirty = [make_sale(sale_id=100 + i, hours_ago=1, suspicious=True) for i in range(30)]

    only_clean = liquidity.compute("C", sales=clean, listings=[], floor_ton=10.0)
    with_dirty = liquidity.compute("C", sales=clean + dirty, listings=[], floor_ton=10.0)

    assert only_clean.sales_24h == with_dirty.sales_24h
    assert only_clean.score == with_dirty.score


def test_bid_support_counts_only_real_offers():
    """Оффер вдвое ниже флора — это не поддержка, а попытка выкупить дёшево."""
    sales = [make_sale(sale_id=i, hours_ago=i) for i in range(1, 20)]

    lowball = liquidity.compute(
        "C", sales=sales, listings=[], floor_ton=10.0, best_offer_ton=3.0
    )
    real = liquidity.compute(
        "C", sales=sales, listings=[], floor_ton=10.0, best_offer_ton=9.0
    )

    assert lowball.parts["bid_support"] == 0.0
    assert real.parts["bid_support"] > 0.5
    assert real.score > lowball.score


def test_expected_tts_derived_from_velocity_when_unmeasured():
    """Без замеров TTS оцениваем через скорость продаж и глубину книги."""
    sales = [make_sale(sale_id=i, hours_ago=i * 6) for i in range(1, 15)]
    listings = [make_listing(listing_id=f"l{i}", price_ton=10.2) for i in range(10)]

    metrics = liquidity.compute("C", sales=sales, listings=listings, floor_ton=10.0)

    assert metrics.tts_median_hours is None
    assert 0 < metrics.expected_tts_hours < 24 * 14


def test_score_stays_in_unit_range():
    sales = [make_sale(sale_id=i, hours_ago=0.5, tts_hours=1.0) for i in range(1, 200)]
    metrics = liquidity.compute(
        "C", sales=sales, listings=[], floor_ton=10.0, best_offer_ton=10.0
    )
    assert 0.0 <= metrics.score <= 1.0


# --- Отсутствие данных против отсутствия ликвидности ---------------------


def test_empty_history_is_marked_as_unmeasured():
    """Балл без единой сделки ничего не измеряет и должен это признавать.

    Раньше все коллекции получали одинаковые 0.17, и это выглядело как
    измерение, хотя означало «данных нет».
    """
    metrics = liquidity.compute("C", sales=[], listings=[], floor_ton=10.0)

    assert not metrics.is_measured
    assert metrics.parts["velocity"] == 0.0


def test_history_makes_score_measured():
    sales = [make_sale(sale_id=i, hours_ago=i) for i in range(1, 10)]
    metrics = liquidity.compute("C", sales=sales, listings=[], floor_ton=10.0)

    assert metrics.is_measured


def test_suspicious_only_history_is_not_measured():
    """Отбракованные антифродом сделки данными не считаются."""
    sales = [make_sale(sale_id=i, hours_ago=i, suspicious=True) for i in range(1, 10)]
    metrics = liquidity.compute("C", sales=sales, listings=[], floor_ton=10.0)

    assert not metrics.is_measured


def test_missing_offer_data_does_not_penalise():
    """Нет источника офферов — вес уходит остальным, а не обнуляет балл.

    Постоянный ноль за недоступный компонент одинаково давил все
    коллекции и уводил под порог отбора даже заведомо ликвидные.
    """
    sales = [make_sale(sale_id=i, hours_ago=i * 2, tts_hours=6.0) for i in range(1, 40)]
    listings = [make_listing(listing_id=f"l{i}", price_ton=10.5, row_id=i) for i in range(5)]

    metrics = liquidity.compute("C", sales=sales, listings=listings, floor_ton=10.0)

    assert "bid_support" in metrics.missing
    # Без перераспределения тот же набор давал бы примерно 0.5.
    assert metrics.score > 0.6


def test_real_offer_data_is_used_not_redistributed():
    sales = [make_sale(sale_id=i, hours_ago=i * 2, tts_hours=6.0) for i in range(1, 40)]
    metrics = liquidity.compute(
        "C", sales=sales, listings=[], floor_ton=10.0, best_offer_ton=9.0
    )

    assert metrics.missing == []
    assert metrics.parts["bid_support"] > 0.5


def test_score_stays_in_range_after_redistribution():
    sales = [make_sale(sale_id=i, hours_ago=0.5, tts_hours=1.0) for i in range(1, 200)]
    metrics = liquidity.compute("C", sales=sales, listings=[], floor_ton=10.0)
    assert 0.0 <= metrics.score <= 1.0
