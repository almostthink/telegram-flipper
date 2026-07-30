"""Извлечение цвета модели из её анимации.

Ни MRKT, ни Portals не отдают цвет модели — только имя и ссылку на
картинку. Но картинка эта не растровая: ``modelStickerKey`` указывает на
``.json``, то есть на Lottie-анимацию, и цвета лежат в ней векторными
заливками в явном виде. Значит, цвет модели берётся разбором JSON, без
декодирования изображений и без единой новой зависимости.

Заливки в Lottie хранятся как ``{"ty": "fl", "c": {"k": [r, g, b, a]}}``,
где составляющие — доли от нуля до единицы. Градиент (``ty: "gf"``)
хранит ленту опорных точек: позиция, затем три составляющие, и так далее.

Обводки (``ty: "st"``) намеренно пропускаем: это контуры и мелкие детали,
обычно чёрные. Считать их цветом подарка — значит объявить монохромными
на чёрном фоне почти все модели подряд.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from app.analytics.color import Color, from_rgb

log = logging.getLogger(__name__)

#: Глубже в структуру анимации не спускаемся. Вложенность Lottie заметно
#: меньше, а ограничение защищает от зацикливания на битом файле.
MAX_DEPTH = 12

#: Сколько заливок разбирать максимум. Сложные анимации содержат тысячи
#: мелких деталей, и цвет подарка определяется задолго до их конца.
MAX_FILLS = 4000


def dominant_color(document: Any) -> Color | None:
    """Основной цвет модели или None, если определить не удалось.

    Выбираем самый частый цветной оттенок. Частота — приближение площади:
    крупная заливка обычно повторяется в кадрах и слоях, а случайный
    блик встречается однажды.

    Ахроматические заливки (чёрное, белое, серое) идут вторым эшелоном.
    В большинстве моделей они есть всегда — это тени, блики и контуры, —
    и если считать их наравне, цветом подарка почти всегда окажется
    чёрный. Но если модель действительно чёрно-белая, брать больше
    неоткуда, и тогда они идут в дело.
    """
    fills = list(_collect_fills(document))
    if not fills:
        return None

    chromatic: Counter = Counter()
    achromatic: Counter = Counter()
    # Считаем по огрублённым корзинам, чтобы соседние оттенки одной
    # заливки не проигрывали сами себе, но возвращаем точный цвет:
    # огрублённый — ничей, его в анимации нет.
    exact: dict[tuple[int, int, int], Counter] = {}

    for color in fills:
        bucket = _bucket(color)
        (achromatic if color.is_achromatic else chromatic)[bucket] += 1
        exact.setdefault(bucket, Counter())[color.rgb] += 1

    chosen = chromatic or achromatic
    if not chosen:
        return None

    winner, _count = chosen.most_common(1)[0]
    red, green, blue = exact[winner].most_common(1)[0][0]
    return from_rgb(red, green, blue)


def _bucket(color: Color) -> tuple[int, int, int]:
    """Огрубляем цвет до шага в 16 значений на канал."""
    return tuple((channel // 16) * 16 + 8 for channel in color.rgb)  # type: ignore[return-value]


def _collect_fills(node: Any, depth: int = 0, budget: list[int] | None = None):
    """Обходим документ и собираем цвета заливок."""
    if budget is None:
        budget = [MAX_FILLS]
    if depth > MAX_DEPTH or budget[0] <= 0:
        return

    if isinstance(node, list):
        for item in node:
            yield from _collect_fills(item, depth + 1, budget)
        return

    if not isinstance(node, dict):
        return

    kind = node.get("ty")
    if kind == "fl":
        color = _solid_color(node.get("c"))
        if color is not None:
            budget[0] -= 1
            yield color
    elif kind == "gf":
        for color in _gradient_colors(node.get("g")):
            budget[0] -= 1
            yield color

    for key, value in node.items():
        # В "c" уже заглянули выше, а строки и числа обходить незачем.
        if key == "c" or not isinstance(value, list | dict):
            continue
        yield from _collect_fills(value, depth + 1, budget)


def _solid_color(node: Any) -> Color | None:
    if not isinstance(node, dict):
        return None
    values = node.get("k")

    # Анимированный цвет: список ключевых кадров, берём первый.
    if isinstance(values, list) and values and isinstance(values[0], dict):
        values = values[0].get("s")

    return _from_components(values)


def _gradient_colors(node: Any):
    """Опорные точки градиента: позиция, затем три составляющие."""
    if not isinstance(node, dict):
        return
    stops = node.get("k")
    if isinstance(stops, dict):
        stops = stops.get("k")
    if isinstance(stops, list) and stops and isinstance(stops[0], dict):
        stops = stops[0].get("s")
    if not isinstance(stops, list):
        return

    count = node.get("p")
    if not isinstance(count, int) or count <= 0:
        count = len(stops) // 4

    for index in range(count):
        offset = index * 4
        color = _from_components(stops[offset + 1 : offset + 4])
        if color is not None:
            yield color


def _from_components(values: Any) -> Color | None:
    """Три доли от нуля до единицы — в цвет."""
    if not isinstance(values, list) or len(values) < 3:
        return None
    try:
        channels = [float(value) for value in values[:3]]
    except (TypeError, ValueError):
        return None

    # Изредка составляющие уже в диапазоне 0..255 — Lottie допускает оба.
    scale = 1.0 if max(channels) > 1.0 else 255.0
    rgb = [int(round(min(max(value * scale, 0.0), 255.0))) for value in channels]
    return from_rgb(*rgb)
