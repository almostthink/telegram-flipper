"""Помощники разбора ответов площадок.

Ни у MRKT, ни у Portals нет опубликованной схемы, а имена полей у них
исторически менялись (``price`` / ``amount`` / ``priceTon``). Поэтому
разбор построен на списках вероятных ключей: адаптер переживает
переименование поля, а не падает целиком.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain import Attribute, AttributeKind, Gift

#: Множители нанотонов: часть API отдаёт цену в нанотонах, часть — в TON.
NANO = 1_000_000_000


def pick(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Первое непустое значение из перечисленных ключей."""
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return default


def to_ton(value: Any) -> float | None:
    """Приводим цену к TON.

    Значения больше миллиона трактуем как нанотоны: реальных лотов
    за миллион TON не существует, а нанотоны — обычный формат TON-API.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number >= 1_000_000:
        return number / NANO
    return number


def to_datetime(value: Any) -> datetime | None:
    """Разбираем время в ISO-строке или unix-таймстампе (сек/мс)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def parse_attribute(raw: Any, kind: AttributeKind) -> Attribute | None:
    """Атрибут приходит либо строкой, либо объектом с редкостью."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return Attribute(kind=kind, name=raw) if raw else None
    if isinstance(raw, dict):
        name = pick(raw, "name", "value", "title", "model", "backdrop", "symbol")
        if not name:
            return None
        rarity = pick(raw, "rarity_per_mille", "rarityPerMille", "rarity", "permille")
        try:
            rarity_value = float(rarity) if rarity is not None else None
        except (TypeError, ValueError):
            rarity_value = None
        # Долю 0..1 приводим к промилле — площадки отдают оба формата.
        if rarity_value is not None and 0 < rarity_value <= 1:
            rarity_value *= 1000
        return Attribute(kind=kind, name=str(name), rarity_permille=rarity_value)
    return None


def parse_gift(raw: dict[str, Any], *, fallback_collection: str = "") -> Gift:
    """Собираем подарок из сырого объекта листинга или события."""
    collection = pick(
        raw, "collection_name", "collectionName", "collection", "gift_name", "name",
        default=fallback_collection,
    )
    if isinstance(collection, dict):
        collection = pick(collection, "name", "title", default=fallback_collection)

    external_id = pick(raw, "id", "nft_id", "nftId", "gift_id", "giftId", "address")
    number = pick(raw, "external_collection_number", "number", "index", "num")
    try:
        number_value = int(number) if number is not None else None
    except (TypeError, ValueError):
        number_value = None

    attributes = pick(raw, "attributes", "traits", default=None)
    model = backdrop = symbol = None

    if isinstance(attributes, list):
        # Формат GetGems: [{"trait_type": "Model", "value": "..."}, ...]
        for item in attributes:
            if not isinstance(item, dict):
                continue
            trait = str(pick(item, "trait_type", "traitType", "type", "name", default="")).lower()
            payload = {"name": pick(item, "value", "name"), **item}
            if "model" in trait:
                model = parse_attribute(payload, AttributeKind.MODEL)
            elif "backdrop" in trait or "background" in trait:
                backdrop = parse_attribute(payload, AttributeKind.BACKDROP)
            elif "symbol" in trait or "pattern" in trait:
                symbol = parse_attribute(payload, AttributeKind.SYMBOL)
    else:
        model = parse_attribute(pick(raw, "model", "modelName"), AttributeKind.MODEL)
        backdrop = parse_attribute(
            pick(raw, "backdrop", "background", "backdropName"), AttributeKind.BACKDROP
        )
        symbol = parse_attribute(
            pick(raw, "symbol", "pattern", "symbolName"), AttributeKind.SYMBOL
        )

    return Gift(
        collection=str(collection or fallback_collection or "unknown"),
        external_id=str(external_id or ""),
        number=number_value,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
    )


def as_list(payload: Any) -> list[dict[str, Any]]:
    """Ответ бывает массивом, бывает объектом с массивом внутри."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "nfts", "list", "activities", "collections"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []
