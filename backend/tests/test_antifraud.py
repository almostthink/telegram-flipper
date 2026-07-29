"""Проверки антифрода."""

from __future__ import annotations

from app.analytics import antifraud

from tests.factories import make_listing, make_sale


def test_clean_history_passes():
    sales = [
        make_sale(sale_id=i, hours_ago=i * 5, buyer=f"buyer{i}", seller=f"seller{i}")
        for i in range(1, 20)
    ]
    assert antifraud.screen_sales(sales).count == 0


def test_detects_round_trip_between_same_pair():
    """Сделки туда-обратно между двумя аккаунтами — классический прогон."""
    sales = []
    for i in range(1, 5):
        sales.append(make_sale(sale_id=i, hours_ago=i, seller="A", buyer="B"))
        sales.append(make_sale(sale_id=100 + i, hours_ago=i + 0.5, seller="B", buyer="A"))

    report = antifraud.screen_sales(sales)
    assert report.count > 0
    assert any("участник" in reason for reason in report.reasons.values())


def test_detects_repeated_flips_of_same_gift():
    """Один и тот же экземпляр, перепроданный четырежды за неделю."""
    sales = [
        make_sale(sale_id=i, hours_ago=i * 12, gift_external_id="gift-777")
        for i in range(1, 5)
    ]
    report = antifraud.screen_sales(sales)
    assert report.count == len(sales)


def test_same_model_different_gifts_is_not_a_flip():
    """Разные экземпляры с одинаковой моделью — это активная торговля.

    Раньше группировка шла по атрибутам, и вся ликвидная коллекция
    целиком помечалась как прогон.
    """
    sales = [
        make_sale(sale_id=i, hours_ago=i * 3, gift_external_id=f"gift-{i}")
        for i in range(1, 12)
    ]
    assert antifraud.screen_sales(sales).count == 0


def test_price_outlier_flagged():
    sales = [make_sale(sale_id=i, price_ton=10.0 + (i % 3), hours_ago=i) for i in range(1, 15)]
    sales.append(make_sale(sale_id=999, price_ton=5000.0, hours_ago=1))

    report = antifraud.screen_sales(sales)
    assert 999 in report.suspicious_sale_ids
    # Нормальные сделки трогать нельзя.
    assert len(report.suspicious_sale_ids) < len(sales)


def test_lone_cheap_listing_is_suspicious():
    """Лот вдвое дешевле кластера — приманка или брак, а не находка."""
    peers = [
        make_listing(listing_id=f"p{i}", price_ton=100.0, row_id=i) for i in range(1, 6)
    ]
    bait = make_listing(listing_id="bait", price_ton=20.0, row_id=99)

    assert antifraud.is_suspicious_listing(bait, [*peers, bait]) is not None
    # Умеренно дешёвый лот — это как раз то, что мы ищем.
    fair = make_listing(listing_id="fair", price_ton=85.0, row_id=98)
    assert antifraud.is_suspicious_listing(fair, [*peers, fair]) is None


def test_suspicion_needs_enough_peers():
    """На двух сопоставимых лотах выводы делать нельзя."""
    peers = [make_listing(listing_id="p1", price_ton=100.0, row_id=1)]
    cheap = make_listing(listing_id="c", price_ton=10.0, row_id=2)
    assert antifraud.is_suspicious_listing(cheap, [*peers, cheap]) is None


def test_seller_concentration():
    listings = [make_listing(listing_id=f"l{i}", seller="whale", row_id=i) for i in range(8)]
    listings += [
        make_listing(listing_id=f"o{i}", seller=f"other{i}", row_id=50 + i) for i in range(2)
    ]

    assert antifraud.seller_concentration(listings) == 0.8
    assert antifraud.seller_concentration([]) == 0.0
