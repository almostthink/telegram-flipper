"""Референсные площадки — только чтение цен.

Торговлю здесь не ведём. Смысл в кросс-маркет сверке: если лот дёшев на
MRKT, но так же дёшев на соседних площадках, то это не скидка, а новый
уровень рынка, и покупать его нельзя. Именно эта проверка отсекает
основную часть ложных сигналов.

Tonnel убран: площадка прекратила работу. Адаптер удалён целиком, а не
выключен флагом — иначе сканер продолжал бы ходить в мёртвый домен,
копить таймауты и засорять журнал ошибками.

GetGems выключен по умолчанию, и REST-адаптер ниже для него не подходит.
Запись трафика показала GraphQL по адресу ``getgems.io/graphql/`` с
persisted queries: текст запроса не передаётся, только его sha256-хеш,
который меняется при каждом обновлении их фронтенда. Коллекции там
адресуются TON-адресами, а не именами, что требует ещё одного справочника.

Воспроизвести это можно, но третий источник цен не стоит такой хрупкости:
Portals и MRKT дают достаточную базу для кросс-маркет сверки.
"""

from __future__ import annotations

import logging

from app.adapters.base import AuthPlacement, EndpointSpec, MarketEndpoints, Marketplace
from app.adapters.parsing import as_list, parse_gift, pick, to_datetime, to_ton
from app.domain import CollectionFloor, Listing, Market

log = logging.getLogger(__name__)

GETGEMS_ENDPOINTS = MarketEndpoints(
    base_url="https://api.getgems.io",
    endpoints={
        "collections": EndpointSpec("/public/api/v1/collections"),
        "listings": EndpointSpec("/public/api/v1/nfts"),
    },
)


class ReadOnlyAdapter(Marketplace):
    """Общая база для площадок, которые нужны только как источник цен."""

    supports_trading = False

    async def collection_floors(self) -> list[CollectionFloor]:
        payload = await self.request("collections", params={"limit": 500})
        floors: list[CollectionFloor] = []

        for raw in as_list(payload):
            name = pick(raw, "name", "title", "collection", "gift_name")
            floor = to_ton(pick(raw, "floor", "floor_price", "floorPrice", "price", "min_price"))
            if not name or floor is None:
                continue
            floors.append(
                CollectionFloor(
                    market=self.name,
                    collection=str(name),
                    floor_ton=floor,
                    volume_24h_ton=to_ton(pick(raw, "volume_24h", "volume24h", "volume")),
                    sales_24h=_int(pick(raw, "sales_24h", "sales24h")),
                    listed_count=_int(pick(raw, "listed", "listed_count", "count")),
                )
            )
        return floors

    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        payload = await self.request(
            "listings",
            params={"collection": collection, "limit": min(limit, 200), "sort": "price_asc"},
        )

        listings: list[Listing] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "amount", "sale_price"))
            if price is None:
                continue
            gift = parse_gift(raw, fallback_collection=collection)
            listing_id = str(pick(raw, "id", "gift_id", "address", default=gift.external_id))
            if not listing_id:
                continue
            listings.append(
                Listing(
                    market=self.name,
                    listing_id=listing_id,
                    gift=gift,
                    price_ton=price,
                    listed_at=to_datetime(pick(raw, "listed_at", "created_at")),
                )
            )
        return listings


class GetGemsAdapter(ReadOnlyAdapter):
    name = Market.GETGEMS
    #: Заголовка Authorization у GetGems нет: сессия живёт в cookie
    #: AUTH_TOKEN и JWT_TOKEN. Публичные цены обычно доступны и без них,
    #: но если площадка потребует вход, вставленная строка cookie уйдёт
    #: в нужный заголовок, а не в игнорируемый Authorization.
    auth_placement = AuthPlacement.COOKIE

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("endpoints", GETGEMS_ENDPOINTS)
        super().__init__(*args, **kwargs)


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
