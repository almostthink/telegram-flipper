"""Монохром в оценке: от карточки лота до надбавки.

Монохром — когда цвет модели совпадает с цветом фона. Цвет фона площадка
отдаёт числом в карточке лота, цвет модели добывается из её анимации и
хранится отдельно. Здесь проверяется, что обе половины сходятся вместе и
доходят до цены.
"""

from __future__ import annotations

import pytest
from app.analytics import pricing
from app.analytics.pricing import PricingContext, monochrome_score
from app.domain import Attribute, AttributeKind, Gift

GREEN = 0x2EA043
DARK_GREEN = 0x145A1E
BLUE = 0x2846BE

MODEL = Attribute(kind=AttributeKind.MODEL, name="Cabbage", rarity_permille=20)


def context(**overrides) -> PricingContext:
    base = {
        "collection": "Lush Bouquet",
        "floor_ton": 10.0,
        "model_colors": {"Cabbage": GREEN},
        "monochrome_bonus": 0.2,
    }
    base.update(overrides)
    return PricingContext(**base)


def gift(backdrop_color: int | None, *, number: int | None = None) -> Gift:
    return Gift(
        collection="Lush Bouquet",
        external_id="G1",
        number=number,
        model=MODEL,
        backdrop=Attribute(kind=AttributeKind.BACKDROP, name="Lemongrass"),
        backdrop_color=backdrop_color,
    )


# --- Определение ----------------------------------------------------------


def test_model_color_matching_backdrop_is_monochrome():
    assert monochrome_score(context(), gift(DARK_GREEN)) > 0.5


def test_different_colors_are_not():
    assert monochrome_score(context(), gift(BLUE)) == 0.0


def test_unknown_model_color_is_not_a_zero():
    """Цвет модели ещё не добыт — это «неизвестно», а не «цвета разные».

    Ноль занижал бы оценку каждого лота, чью модель мы ещё не разобрали,
    и монохромные лоты выглядели бы как обычные, пока очередь не дойдёт.
    """
    assert monochrome_score(context(model_colors={}), gift(DARK_GREEN)) is None


def test_missing_backdrop_color_is_unknown_too():
    assert monochrome_score(context(), gift(None)) is None


def test_gift_without_model_is_unknown():
    bare = Gift(collection="Lush Bouquet", external_id="G1", backdrop_color=DARK_GREEN)
    assert monochrome_score(context(), bare) is None


# --- Надбавка -------------------------------------------------------------


def test_monochrome_raises_the_baseline():
    premium, parts = pricing.collectible_premium(context(), gift(DARK_GREEN))
    plain, plain_parts = pricing.collectible_premium(context(), gift(BLUE))

    assert premium > plain
    assert parts["premium_monochrome"] > 0
    assert plain_parts["premium_monochrome"] == 0


def test_unknown_color_does_not_lower_the_price():
    """Неизвестность не должна работать как отрицательный признак."""
    unknown, _ = pricing.collectible_premium(context(model_colors={}), gift(DARK_GREEN))
    mismatch, _ = pricing.collectible_premium(context(), gift(BLUE))

    assert unknown == mismatch, "оба без надбавки, но не ниже базы"
    assert unknown >= 1.0


def test_bonus_is_configurable_and_can_be_switched_off():
    off = context(monochrome_bonus=0.0)
    premium, parts = pricing.collectible_premium(off, gift(DARK_GREEN))

    assert parts["premium_monochrome"] == 0
    assert premium == pytest.approx(1.0)


def test_partial_match_gives_partial_bonus():
    """Близкий, но заметно другой оттенок — между «в тон» и «мимо»."""
    exact, exact_parts = pricing.collectible_premium(context(), gift(GREEN))
    near, near_parts = pricing.collectible_premium(
        context(model_colors={"Cabbage": 0x2E9043}), gift(0x35B04A)
    )

    assert 0 < near_parts["premium_monochrome"] <= exact_parts["premium_monochrome"]
    assert near <= exact


# --- Номер выпуска --------------------------------------------------------


def test_number_bonus_actually_reaches_the_price():
    """Номер не доходил до оценки: listing_to_gift его не передавал.

    Надбавка за красивый номер считалась от gift.number, который всегда
    был пустым, поэтому настройка number_bonus молча ничего не делала.
    """
    from app.analytics.signals import listing_to_gift

    from tests.factories import make_listing

    listing = make_listing(listing_id="L1")
    listing.number = 1
    listing.backdrop_color = DARK_GREEN

    converted = listing_to_gift(listing)
    assert converted.number == 1, "номер обязан доезжать до оценки"
    assert converted.backdrop_color == DARK_GREEN

    with_number = pricing.collectible_premium(
        context(number_bonus=0.35), gift(BLUE, number=1)
    )[0]
    without = pricing.collectible_premium(
        context(number_bonus=0.35), gift(BLUE, number=40597)
    )[0]
    assert with_number > without
