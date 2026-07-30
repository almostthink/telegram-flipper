"""Цвет подарка и фона: сравнение и признак монохрома.

Монохром — это когда цвет модели совпадает с цветом фона. Рынок платит за
такое сочетание надбавку сверх редкости обеих частей по отдельности:
редкая модель на случайном фоне и та же модель «в тон» стоят по-разному.

Что известно точно, а что нет:

* **Цвет фона площадка отдаёт числом.** В карточке лота лежат
  ``backdropColorsCenterColor`` и ``backdropColorsEdgeColor`` — целые
  0xRRGGBB. Здесь гадать не о чем.
* **Цвета модели не отдаёт никто.** Ни MRKT, ни Portals: в ответах есть
  только имя модели и ссылка на её картинку. Поэтому цвет модели
  добывается отдельно (см. ``app.ingest.model_colors``), а этот модуль
  занимается только сравнением.

Сравниваем по тону в HSV, а не по расстоянию между RGB. Расстояние в RGB
не соответствует восприятию: тёмно-зелёный и тёмно-синий numerically
близки, а глазом это разные цвета; светло-зелёный и тёмно-зелёный
далеки, а для «в тон» это одно и то же.

Отдельная ветка нужна для чёрного, белого и серого. У них тон не
определён — угол на цветовом круге при нулевой насыщенности произволен, и
сравнивать его бессмысленно. Такие цвета сравниваются по светлоте.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

#: Ниже этой насыщенности цвет считается серым: тон у него случайный.
ACHROMATIC_SATURATION = 0.15
#: Ниже этой яркости цвет считается чёрным независимо от тона.
DARK_VALUE = 0.16
#: Выше этой яркости при низкой насыщенности — белый.
LIGHT_VALUE = 0.90

#: Разница тонов в градусах, при которой цвета ещё «в тон». Соседние
#: оттенки одного цвета укладываются в этот сектор, соседние цвета —
#: уже нет: от зелёного до жёлтого на круге около шестидесяти градусов.
HUE_TOLERANCE_DEG = 22.0
#: Насколько может отличаться светлота у серых, чтобы считаться совпадением.
GREY_VALUE_TOLERANCE = 0.22


@dataclass(frozen=True, slots=True)
class Color:
    """Цвет в двух представлениях сразу — так удобнее сравнивать."""

    rgb: tuple[int, int, int]
    hue_deg: float
    saturation: float
    value: float

    @property
    def is_achromatic(self) -> bool:
        """Чёрный, белый или серый: тон не несёт смысла."""
        return (
            self.saturation < ACHROMATIC_SATURATION
            or self.value < DARK_VALUE
            or (self.value > LIGHT_VALUE and self.saturation < ACHROMATIC_SATURATION * 2)
        )

    @property
    def as_int(self) -> int:
        red, green, blue = self.rgb
        return (red << 16) | (green << 8) | blue


def from_int(value: int | None) -> Color | None:
    """Разбор цвета из целого 0xRRGGBB, как его отдаёт площадка."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 0xFFFFFF:
        return None
    return from_rgb((number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF)


def from_rgb(red: int, green: int, blue: int) -> Color:
    hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    return Color(
        rgb=(red, green, blue),
        hue_deg=hue * 360.0,
        saturation=saturation,
        value=value,
    )


def hue_distance(first: float, second: float) -> float:
    """Расстояние между тонами по кругу: 350° и 10° различаются на 20°."""
    raw = abs(first - second) % 360.0
    return min(raw, 360.0 - raw)


def match_score(model: Color | None, backdrop: Color | None) -> float | None:
    """Насколько цвет модели совпадает с цветом фона: 0..1.

    None означает «неизвестно» — цвета одной из сторон нет. Это не то же
    самое, что ноль: отсутствие данных не должно выглядеть как уверенное
    «не монохром» и попадать в оценку как отрицательный признак.

    Возвращаем не «да/нет», а долю. Совпадение цвета бывает частичным —
    близкий, но заметно другой оттенок; резкая граница на произвольном
    пороге давала бы скачок цены там, где рынок видит плавный переход.
    """
    if model is None or backdrop is None:
        return None

    if model.is_achromatic != backdrop.is_achromatic:
        # Цветное на сером (или наоборот) — противоположность монохрома.
        return 0.0

    if model.is_achromatic:
        # Оба серые: чёрное на чёрном — монохром, чёрное на белом — нет.
        gap = abs(model.value - backdrop.value)
        return _falloff(gap, GREY_VALUE_TOLERANCE)

    gap = hue_distance(model.hue_deg, backdrop.hue_deg)
    score = _falloff(gap, HUE_TOLERANCE_DEG)
    if score <= 0:
        return 0.0

    # Тон совпал, но блёклое на насыщенном смотрится не «в тон».
    saturation_gap = abs(model.saturation - backdrop.saturation)
    return round(score * _falloff(saturation_gap, 0.55), 4)


def is_monochrome(model: Color | None, backdrop: Color | None, *, threshold: float) -> bool:
    score = match_score(model, backdrop)
    return score is not None and score >= threshold


def _falloff(gap: float, tolerance: float) -> float:
    """1 при полном совпадении, 0 за пределами допуска, линейно между."""
    if tolerance <= 0:
        return 1.0 if gap == 0 else 0.0
    return round(max(0.0, 1.0 - gap / tolerance), 4)
