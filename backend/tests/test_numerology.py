"""Проверки коллекционной ценности номера и фона."""

from __future__ import annotations

import pytest
from app.analytics import numerology, pricing
from app.analytics.pricing import PricingContext
from app.analytics.signals import is_collectible
from app.config import CollectibleConfig
from app.domain import Attribute, AttributeKind, Gift

from tests.factories import make_listing

# --- Разбор номера -------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "expected_label_part"),
    [
        (1, "первый"),
        (7, "однозначный"),
        (42, "двузначный"),
        (777, "повтор"),
        (1111, "повтор"),
        (9999, "повтор"),
        (1000, "круглый"),
        (10000, "круглый"),
        (1234, "последовательность"),
        (4321, "последовательность"),
        (1221, "палиндром"),
        (1212, "парный"),
        (6969, "парный"),
    ],
)
def test_recognises_notable_numbers(number: int, expected_label_part: str):
    trait = numerology.classify(number)
    assert expected_label_part in trait.label
    assert trait.is_notable, f"#{number} должен считаться заметным"


@pytest.mark.parametrize("number", [40597, 23814, 5163, 88231])
def test_ordinary_numbers_get_no_premium(number: int):
    trait = numerology.classify(number)
    assert trait.score == 0.0
    assert not trait.is_notable


def test_first_beats_everything():
    """#1 — отдельная категория, дороже любого узора."""
    assert numerology.score(1) > numerology.score(1111)
    assert numerology.score(1) == 1.0


def test_longer_repdigit_scores_higher():
    """Чем длиннее повтор, тем он реже."""
    assert numerology.score(9999) > numerology.score(999)


def test_missing_number_is_safe():
    assert numerology.score(None) == 0.0
    assert numerology.score(0) == 0.0
    assert numerology.score(-5) == 0.0


def test_best_trait_wins_not_sum():
    """1000 одновременно круглый и четырёхзначный — берём лучшее свойство."""
    trait = numerology.classify(1000)
    assert trait.score <= 1.0
    assert "круглый" in trait.label


# --- Влияние на оценку цены ----------------------------------------------


def context(**overrides) -> PricingContext:
    base = {
        "collection": "Plush Pepe",
        "floor_ton": 10.0,
        "attribute_floors": {("model", "Common"): 10.0},
        "number_bonus": 0.35,
        "preferred_backdrops": ["Black"],
        "backdrop_bonus": 0.15,
    }
    base.update(overrides)
    return PricingContext(**base)


def gift(number: int | None = None, backdrop: str | None = None) -> Gift:
    return Gift(
        collection="Plush Pepe",
        external_id="g1",
        number=number,
        model=Attribute(kind=AttributeKind.MODEL, name="Common", rarity_permille=300),
        backdrop=(
            Attribute(kind=AttributeKind.BACKDROP, name=backdrop) if backdrop else None
        ),
    )


def test_nice_number_raises_baseline_value():
    ordinary, _ = pricing.baseline_value(context(), gift(40597))
    special, components = pricing.baseline_value(context(), gift(1111))

    assert special > ordinary
    assert components["premium_number"] > 0
    # Надбавка ограничена настройкой: без потолка красивый номер уводил бы
    # оценку сколь угодно далеко от рынка.
    assert special / ordinary <= 1 + 0.35


def test_first_number_gets_full_bonus():
    _, components = pricing.baseline_value(context(), gift(1))
    assert components["premium_number"] == pytest.approx(0.35)


def test_preferred_backdrop_raises_value():
    plain, _ = pricing.baseline_value(context(), gift(500, backdrop="Ocean"))
    black, components = pricing.baseline_value(context(), gift(500, backdrop="Black"))

    assert black > plain
    assert components["premium_backdrop"] == pytest.approx(0.15)


def test_backdrop_match_is_case_insensitive():
    _, components = pricing.baseline_value(
        context(preferred_backdrops=["black"]), gift(500, backdrop="Black")
    )
    assert components["premium_backdrop"] > 0


def test_bonuses_add_not_multiply():
    """Красивый номер и ценный фон усиливают друг друга, но не кратно."""
    _, both = pricing.baseline_value(context(), gift(1, backdrop="Black"))
    assert both["collectible_premium"] == pytest.approx(1 + 0.35 + 0.15)


def test_zero_bonus_disables_number_completely():
    plain, _ = pricing.baseline_value(context(number_bonus=0.0), gift(40597))
    special, _ = pricing.baseline_value(context(number_bonus=0.0), gift(1))
    assert plain == special


def test_number_is_a_regression_feature():
    """Модель должна выучивать надбавку за номер по фактическим сделкам."""
    from tests.factories import sales_population

    sales = sales_population(80)
    for index, sale in enumerate(sales):
        # Половине сделок даём красивые номера и на 60% более высокую цену.
        if index % 2 == 0:
            sale.number = 1111
            sale.price_ton *= 1.6
        else:
            sale.number = 40000 + index

    fit = pricing.fit_regression(context(sales=sales))
    assert fit is not None

    # Коэффициент при номере — пятый в векторе признаков, и он обязан
    # быть заметно положительным.
    assert fit.coefficients[4] > 0.2


# --- Фильтр «только коллекционное» ---------------------------------------


def test_collectible_filter_accepts_nice_number():
    listing = make_listing(listing_id="a", price_ton=10.0)
    listing.number = 7777
    assert is_collectible(listing, CollectibleConfig())


def test_collectible_filter_accepts_preferred_backdrop():
    listing = make_listing(listing_id="b", price_ton=10.0)
    listing.number = 40597
    listing.backdrop = "Black"
    assert is_collectible(listing, CollectibleConfig(preferred_backdrops=["Black"]))


def test_collectible_filter_rejects_plain_lot():
    listing = make_listing(listing_id="c", price_ton=10.0)
    listing.number = 40597
    listing.backdrop = "Ocean"
    assert not is_collectible(listing, CollectibleConfig(preferred_backdrops=["Black"]))
