"""Проверки сравнения цветов.

Монохром — когда цвет модели совпадает с цветом фона. Цвет фона площадка
отдаёт числом, цвет модели приходится добывать отдельно, а здесь
проверяется только само сравнение: оно должно совпадать с тем, что видит
глаз, а не с арифметикой над RGB.
"""

from __future__ import annotations

import pytest
from app.analytics import color as color_mod
from app.analytics.color import from_int, from_rgb, hue_distance, is_monochrome, match_score

GREEN = from_rgb(46, 160, 67)
DARK_GREEN = from_rgb(20, 90, 30)
BLUE = from_rgb(40, 70, 190)
RED = from_rgb(200, 40, 40)
BLACK = from_rgb(12, 12, 14)
CHARCOAL = from_rgb(35, 35, 38)
WHITE = from_rgb(245, 245, 245)
GREY = from_rgb(128, 128, 130)


# --- Разбор ---------------------------------------------------------------


def test_int_from_marketplace_is_parsed_as_rgb():
    """Площадка отдаёт цвет фона целым 0xRRGGBB."""
    parsed = from_int(0x2EA043)
    assert parsed is not None
    assert parsed.rgb == (0x2E, 0xA0, 0x43)


def test_real_backdrop_value_round_trips():
    """11450458 — настоящий backdropColorsCenterColor из ленты MRKT."""
    parsed = from_int(11450458)
    assert parsed is not None
    assert parsed.as_int == 11450458


def test_garbage_is_not_a_color():
    for bad in (None, -1, 0x1000000, "зелёный", object()):
        assert from_int(bad) is None  # type: ignore[arg-type]


# --- Тон ------------------------------------------------------------------


def test_hue_distance_wraps_around_the_circle():
    """350° и 10° — соседние оттенки красного, а не противоположности."""
    assert hue_distance(350.0, 10.0) == pytest.approx(20.0)
    assert hue_distance(10.0, 350.0) == pytest.approx(20.0)


# --- Совпадение -----------------------------------------------------------


def test_same_hue_is_monochrome():
    assert match_score(GREEN, DARK_GREEN) > 0.5


def test_different_hues_are_not():
    assert match_score(GREEN, BLUE) == 0.0
    assert match_score(RED, GREEN) == 0.0


def test_rgb_distance_would_have_been_wrong():
    """Проверка того, ради чего взят HSV, а не расстояние в RGB.

    Тёмно-зелёный и тёмно-синий близки численно, но глазом это разные
    цвета. Светло-зелёный и тёмно-зелёный численно далеки, а для «в тон»
    это один цвет.
    """
    dark_blue = from_rgb(20, 30, 90)
    light_green = from_rgb(150, 240, 160)

    rgb_gap = sum(abs(a - b) for a, b in zip(DARK_GREEN.rgb, dark_blue.rgb, strict=True))
    far_rgb_gap = sum(abs(a - b) for a, b in zip(DARK_GREEN.rgb, light_green.rgb, strict=True))
    assert rgb_gap < far_rgb_gap, "по RGB ближе оказывается не тот цвет"

    assert match_score(DARK_GREEN, dark_blue) == 0.0
    assert match_score(DARK_GREEN, light_green) > 0.0


# --- Чёрное, белое, серое -------------------------------------------------


def test_black_on_black_is_monochrome():
    """У серых тон не определён — сравнивать надо светлоту."""
    assert match_score(BLACK, CHARCOAL) > 0.5


def test_black_on_white_is_not():
    assert match_score(BLACK, WHITE) == 0.0


def test_colored_on_grey_is_the_opposite_of_monochrome():
    assert match_score(GREEN, GREY) == 0.0
    assert match_score(GREY, GREEN) == 0.0


def test_near_black_counts_as_achromatic_despite_hue():
    """У почти чёрного тон случаен и сравнению не подлежит."""
    almost_black_green = from_rgb(6, 14, 8)
    assert almost_black_green.is_achromatic


# --- Насыщенность ---------------------------------------------------------


def test_washed_out_on_vivid_scores_lower_than_exact_match():
    """Тон тот же, но блёклое на насыщенном смотрится не совсем в тон."""
    vivid = from_rgb(0, 220, 0)
    pale = from_rgb(150, 210, 155)

    assert match_score(vivid, pale) < match_score(vivid, from_rgb(0, 190, 0))


# --- Неизвестность --------------------------------------------------------


def test_unknown_color_is_not_a_zero():
    """Отсутствие данных о цвете модели не должно выглядеть как «не монохром».

    Ноль — это утверждение, что цвета разные, и в оценке он работает как
    отрицательный признак. Когда цвет модели просто не добыт, честный
    ответ — «неизвестно».
    """
    assert match_score(None, GREEN) is None
    assert match_score(GREEN, None) is None
    assert is_monochrome(None, GREEN, threshold=0.5) is False


def test_threshold_is_configurable():
    assert is_monochrome(GREEN, DARK_GREEN, threshold=0.1) is True
    assert is_monochrome(GREEN, DARK_GREEN, threshold=0.99) is False


def test_identical_colors_score_one():
    assert match_score(GREEN, GREEN) == pytest.approx(1.0)
    assert match_score(BLACK, BLACK) == pytest.approx(1.0)


def test_tolerance_constants_stay_within_one_color_sector():
    """Между зелёным и жёлтым около шестидесяти градусов.

    Допуск шире половины этого расстояния начал бы считать соседние
    цвета одним, и «монохром» перестал бы что-либо означать.
    """
    assert color_mod.HUE_TOLERANCE_DEG < 30.0
