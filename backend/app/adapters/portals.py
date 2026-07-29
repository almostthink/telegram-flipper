"""Адаптер Portals (portal-market.com).

Пути и имена полей восстановлены из реальной записи трафика мини-аппа.

Прежняя версия обращалась к ``portals-market.com`` — такого домена не
существует, и запросы падали с ошибкой разрешения имени. Настоящий адрес
пишется с дефисом иначе: **portal-market.com**.

Две особенности, отличающие площадку от MRKT:

**Цены — строки в TON, а не нанотоны.** ``"172.69"``, не ``172690000000``.
Переводить нечего, но разбирать надо как строку.

**Коллекции адресуются идентификатором, а не именем.** ``/api/nfts/search``
принимает ``collection_ids`` с UUID, поэтому адаптер держит соответствие
имени и идентификатора, полученное из ``/api/collections``.

Торговля здесь не ведётся: подтверждены только запросы на чтение, а
покупать по неподтверждённому пути нельзя. Площадка нужна как второй
источник цен для кросс-маркет сверки.
"""

from __future__ import annotations

import logging

from app.adapters.base import EndpointSpec, MarketEndpoints, Marketplace
from app.adapters.parsing import as_list, pick, to_datetime, to_ton
from app.domain import (
    Attribute,
    AttributeFloor,
    AttributeKind,
    CollectionFloor,
    Gift,
    Listing,
    Market,
)

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = MarketEndpoints(
    base_url="https://portal-market.com",
    endpoints={
        # --- подтверждено записью трафика ---
        "collections": EndpointSpec("/api/collections", json_path="collections"),
        "search": EndpointSpec("/api/nfts/search", json_path="results"),
        "symbols": EndpointSpec("/api/collections/filters/symbols"),
        # --- не подтверждено: в записи этих срезов не было ---
        # Предположение по симметрии с filters/symbols.
        "models": EndpointSpec("/api/collections/filters/models"),
        "backdrops": EndpointSpec("/api/collections/filters/backdrops"),
    },
)

#: Соответствие имени атрибута в ответе и нашей оси редкости.
_ATTRIBUTE_KINDS = {
    "model": AttributeKind.MODEL,
    "backdrop": AttributeKind.BACKDROP,
    "background": AttributeKind.BACKDROP,
    "symbol": AttributeKind.SYMBOL,
    "pattern": AttributeKind.SYMBOL,
}


def parse_portals_gift(raw: dict, *, fallback_collection: str = "") -> Gift:
    """Разбор подарка. Атрибуты приходят списком ``{type, value, rarity_per_mille}``."""
    model = backdrop = symbol = None

    for item in raw.get("attributes") or []:
        if not isinstance(item, dict):
            continue
        kind = _ATTRIBUTE_KINDS.get(str(pick(item, "type", "trait_type", default="")).lower())
        value = pick(item, "value", "name")
        if kind is None or not value:
            continue
        attribute = Attribute(
            kind=kind,
            name=str(value),
            rarity_permille=_as_float(pick(item, "rarity_per_mille", "rarityPerMille")),
        )
        if kind is AttributeKind.MODEL:
            model = attribute
        elif kind is AttributeKind.BACKDROP:
            backdrop = attribute
        else:
            symbol = attribute

    number = pick(raw, "external_collection_number")
    try:
        number_value = int(number) if number is not None else None
    except (TypeError, ValueError):
        number_value = None

    return Gift(
        collection=str(pick(raw, "name", default=fallback_collection)),
        external_id=str(pick(raw, "id", default="")),
        number=number_value,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
    )


class PortalsAdapter(Marketplace):
    name = Market.PORTALS
    #: Торговля не подтверждена записью — только чтение цен для сверки.
    supports_trading = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: Имя коллекции → её идентификатор. Заполняется при сборе флоров,
        #: потому что поиск лотов принимает именно идентификатор.
        self._collection_ids: dict[str, str] = {}

    async def collection_floors(self) -> list[CollectionFloor]:
        payload = await self.request(
            "collections", params={"limit": 150, "offset": 0, "favorites_only": "false"}
        )
        floors: list[CollectionFloor] = []

        for raw in as_list(payload):
            name = pick(raw, "name")
            floor = to_ton(pick(raw, "floor_price"))
            if not name or floor is None:
                continue

            collection_id = pick(raw, "id")
            if collection_id:
                self._collection_ids[str(name)] = str(collection_id)

            floors.append(
                CollectionFloor(
                    market=self.name,
                    collection=str(name),
                    floor_ton=floor,
                    volume_24h_ton=to_ton(pick(raw, "day_volume")),
                    sales_24h=_as_int(pick(raw, "sales_24h_count")),
                    listed_count=_as_int(pick(raw, "listed_count")),
                )
            )
        return floors

    async def collection_id(self, collection: str) -> str | None:
        """Идентификатор коллекции, при необходимости обновив справочник."""
        if collection not in self._collection_ids:
            await self.collection_floors()
        return self._collection_ids.get(collection)

    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        collection_id = await self.collection_id(collection)
        if collection_id is None:
            log.debug("Portals: коллекция %s не найдена в справочнике", collection)
            return []

        payload = await self.request(
            "search",
            params={
                "offset": 0,
                "limit": min(limit, 50),
                "collection_ids": collection_id,
                "sort_by": "price asc",
                "status": "listed",
                "exclude_bundled": "true",
                "premarket_status": "all",
            },
        )

        listings: list[Listing] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price"))
            if price is None or pick(raw, "is_owned") is True:
                continue

            gift = parse_portals_gift(raw, fallback_collection=collection)
            if not gift.external_id:
                continue
            gift.collection = collection

            listings.append(
                Listing(
                    market=self.name,
                    listing_id=gift.external_id,
                    gift=gift,
                    price_ton=price,
                    listed_at=to_datetime(pick(raw, "listed_at")),
                    url=f"https://t.me/portals/market?startapp={gift.external_id}",
                    # unlocks_at — момент, до которого лот нельзя перепродать.
                    resale_available_at=to_datetime(pick(raw, "unlocks_at")),
                )
            )
        return listings

    async def attribute_floors(self, collection: str) -> list[AttributeFloor]:
        """Флоры по срезам атрибутов.

        Подтверждён только эндпоинт символов; модели и фоны — предположение
        по симметрии. Недоступный срез пропускается, остальные работают.
        """
        result: list[AttributeFloor] = []
        groups = (
            ("symbols", AttributeKind.SYMBOL),
            ("models", AttributeKind.MODEL),
            ("backdrops", AttributeKind.BACKDROP),
        )

        for endpoint, kind in groups:
            try:
                payload = await self.request(endpoint)
            except Exception as exc:  # noqa: BLE001 — один срез не роняет остальные
                log.debug("Portals/%s: %s", endpoint, exc)
                continue

            for raw in as_list(payload):
                name = pick(raw, "name", "value")
                floor = to_ton(pick(raw, "floor_price", "min_price"))
                if not name or floor is None:
                    continue
                result.append(
                    AttributeFloor(
                        market=self.name,
                        collection=collection,
                        kind=kind,
                        name=str(name),
                        floor_ton=floor,
                        rarity_permille=_as_float(pick(raw, "rarity_per_mille")),
                    )
                )
        return result


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
