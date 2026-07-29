"""Редкость атрибутов и её перевод в числовые признаки.

Цена подарка нелинейна по редкости: разница между 50‰ и 40‰ почти не
видна, а между 5‰ и 1‰ — кратная. Поэтому везде работаем с логарифмом
редкости, а не с самой долей.
"""

from __future__ import annotations

import math

from app.domain import Attribute, AttributeKind, Gift

#: Чем заменяем неизвестную редкость. 100‰ = 10% — примерно медиана
#: по обычным атрибутам, то есть нейтральное предположение.
DEFAULT_PERMILLE = 100.0

#: Ниже этого значения редкость считаем шумом: площадки иногда отдают 0.
MIN_PERMILLE = 0.1


def rarity_score(attribute: Attribute | None) -> float:
    """Признак редкости: чем реже атрибут, тем больше значение.

    ``-ln(доля)``: 100‰ даёт 2.30, 10‰ — 4.61, 1‰ — 6.91.
    """
    permille = DEFAULT_PERMILLE
    if attribute is not None and attribute.rarity_permille is not None:
        permille = max(attribute.rarity_permille, MIN_PERMILLE)
    return -math.log(permille / 1000.0)


def rarity_scores(gift: Gift) -> tuple[float, float, float]:
    """Признаки редкости по трём осям: модель, фон, символ."""
    return (
        rarity_score(gift.model),
        rarity_score(gift.backdrop),
        rarity_score(gift.symbol),
    )


def known_rarity_count(gift: Gift) -> int:
    """Сколько осей редкости известны — влияет на доверие к оценке."""
    return sum(
        1
        for attribute in (gift.model, gift.backdrop, gift.symbol)
        if attribute is not None and attribute.rarity_permille is not None
    )


def describe(gift: Gift) -> str:
    """Человекочитаемое описание для журнала и интерфейса."""
    parts = []
    for kind in (AttributeKind.MODEL, AttributeKind.BACKDROP, AttributeKind.SYMBOL):
        attribute = gift.attribute(kind)
        if attribute is None:
            continue
        if attribute.rarity_permille is not None:
            parts.append(f"{attribute.name} ({attribute.rarity_permille:.1f}‰)")
        else:
            parts.append(attribute.name)
    return " · ".join(parts) if parts else "без атрибутов"
