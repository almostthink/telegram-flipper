"""Адаптер Portals (portals-market.com).

Пути эндпоинтов соответствуют публично описанному поведению мини-аппа
(см. открытый модуль portalsmp). Официальной документации у площадки нет,
поэтому любой путь переопределяется через конфиг и HAR-импорт.

Авторизация — заголовок ``tma <initData>``, который выдаёт Telegram при
открытии мини-аппа. Живёт 1-7 дней, обновляется в app/auth/tma.py.
"""

from __future__ import annotations

import logging

from app.adapters.base import EndpointSpec, MarketEndpoints, Marketplace
from app.adapters.parsing import as_list, parse_gift, pick, to_datetime, to_ton
from app.domain import (
    ActivityEvent,
    ActivityKind,
    AttributeFloor,
    AttributeKind,
    Balance,
    CollectionFloor,
    CollectionOffer,
    Listing,
    Market,
    OwnedGift,
)

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = MarketEndpoints(
    base_url="https://portals-market.com/api",
    endpoints={
        "search": EndpointSpec("/nfts/search"),
        "collections": EndpointSpec("/collections"),
        "collection_floors": EndpointSpec("/collections/floors"),
        "filter_floors": EndpointSpec("/collections/filters"),
        "activity": EndpointSpec("/market/actions/"),
        "offers": EndpointSpec("/offers/collection"),
        "balance": EndpointSpec("/users/balance"),
        "inventory": EndpointSpec("/nfts/owned"),
        "buy": EndpointSpec("/nfts/buy", method="POST"),
        "sell": EndpointSpec("/nfts/list", method="POST"),
        "change_price": EndpointSpec("/nfts/price", method="POST"),
        "delist": EndpointSpec("/nfts/unlist", method="POST"),
    },
)

_ACTION_MAP = {
    "buy": ActivityKind.SALE,
    "sale": ActivityKind.SALE,
    "sold": ActivityKind.SALE,
    "listing": ActivityKind.LISTING,
    "list": ActivityKind.LISTING,
    "price_update": ActivityKind.PRICE_UPDATE,
    "offer": ActivityKind.OFFER,
    "delist": ActivityKind.DELIST,
    "unlist": ActivityKind.DELIST,
}


class PortalsAdapter(Marketplace):
    name = Market.PORTALS
    supports_trading = True

    async def collection_floors(self) -> list[CollectionFloor]:
        payload = await self.request("collections", params={"limit": 500})
        floors: list[CollectionFloor] = []

        for raw in as_list(payload):
            name = pick(raw, "name", "title", "collection_name")
            floor = to_ton(pick(raw, "floor_price", "floorPrice", "floor"))
            if not name or floor is None:
                continue
            floors.append(
                CollectionFloor(
                    market=self.name,
                    collection=str(name),
                    floor_ton=floor,
                    volume_24h_ton=to_ton(pick(raw, "volume_24h", "volume24h", "daily_volume")),
                    sales_24h=_as_int(pick(raw, "sales_24h", "sales24h", "daily_sales")),
                    listed_count=_as_int(pick(raw, "listed_count", "listedCount", "supply_listed")),
                )
            )
        return floors

    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        payload = await self.request(
            "search",
            params={
                "offset": 0,
                "limit": min(limit, 200),
                "filter_by_collections": collection,
                "sort_by": "price asc",
                "status": "listed",
            },
        )

        listings: list[Listing] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "amount", "price_ton"))
            if price is None:
                continue
            gift = parse_gift(raw, fallback_collection=collection)
            listing_id = str(pick(raw, "id", "nft_id", "listing_id", default=gift.external_id))
            if not listing_id:
                continue
            listings.append(
                Listing(
                    market=self.name,
                    listing_id=listing_id,
                    gift=gift,
                    price_ton=price,
                    seller=_as_str(pick(raw, "seller", "owner", "seller_id")),
                    listed_at=to_datetime(pick(raw, "listed_at", "listedAt", "created_at")),
                    url=f"https://t.me/portals/market?startapp=gift_{listing_id}",
                )
            )
        return listings

    async def attribute_floors(self, collection: str) -> list[AttributeFloor]:
        """Флоры по моделям, фонам и символам — самый ценный источник для оценки.

        Ответ ожидается вида ``{"models": {...}, "backdrops": {...}, "symbols": {...}}``.
        """
        payload = await self.request("filter_floors", params={"collection_name": collection})
        if not isinstance(payload, dict):
            return []

        groups = {
            AttributeKind.MODEL: pick(payload, "models", "model", default={}),
            AttributeKind.BACKDROP: pick(payload, "backdrops", "backdrop", default={}),
            AttributeKind.SYMBOL: pick(payload, "symbols", "symbol", default={}),
        }

        result: list[AttributeFloor] = []
        for kind, group in groups.items():
            for name, value in _iter_named(group):
                raw_floor = pick(value, "floor", "price") if isinstance(value, dict) else value
                floor = to_ton(raw_floor)
                if floor is None:
                    continue
                rarity = None
                if isinstance(value, dict):
                    rarity = pick(value, "rarity_per_mille", "rarity")
                result.append(
                    AttributeFloor(
                        market=self.name,
                        collection=collection,
                        kind=kind,
                        name=str(name),
                        floor_ton=floor,
                        rarity_permille=_as_float(rarity),
                    )
                )
        return result

    async def activity(
        self, collection: str | None = None, *, limit: int = 100
    ) -> list[ActivityEvent]:
        params: dict[str, object] = {"offset": 0, "limit": min(limit, 200)}
        if collection:
            params["filter_by_collections"] = collection

        payload = await self.request("activity", params=params)
        events: list[ActivityEvent] = []

        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "amount"))
            happened = to_datetime(pick(raw, "created_at", "createdAt", "timestamp", "date"))
            if price is None or happened is None:
                continue
            action = str(pick(raw, "type", "action", "activity_type", default="")).lower()
            nft = raw.get("nft") if isinstance(raw.get("nft"), dict) else raw
            events.append(
                ActivityEvent(
                    market=self.name,
                    kind=_ACTION_MAP.get(action, ActivityKind.LISTING),
                    gift=parse_gift(nft, fallback_collection=collection or ""),
                    price_ton=price,
                    happened_at=happened,
                    external_id=_as_str(pick(raw, "id", "action_id")),
                    buyer=_as_str(pick(raw, "buyer", "buyer_id", "to")),
                    seller=_as_str(pick(raw, "seller", "seller_id", "from")),
                )
            )
        return events

    async def collection_offers(self, collection: str) -> list[CollectionOffer]:
        payload = await self.request("offers", params={"collection_name": collection})
        offers: list[CollectionOffer] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "amount"))
            if price is None:
                continue
            offers.append(
                CollectionOffer(
                    market=self.name,
                    collection=collection,
                    price_ton=price,
                    amount=_as_int(pick(raw, "amount", "count", "quantity")) or 1,
                )
            )
        return offers

    # --- Торговля -------------------------------------------------------

    async def balance(self) -> Balance:
        payload = await self.request("balance")
        raw = payload if isinstance(payload, dict) else {}
        return Balance(market=self.name, ton=to_ton(pick(raw, "ton", "balance", "amount")) or 0.0)

    async def inventory(self) -> list[OwnedGift]:
        payload = await self.request("inventory", params={"limit": 200})
        owned: list[OwnedGift] = []
        for raw in as_list(payload):
            gift = parse_gift(raw)
            price = to_ton(pick(raw, "price", "amount"))
            status = str(pick(raw, "status", "state", default="")).lower()
            owned.append(
                OwnedGift(
                    market=self.name,
                    gift=gift,
                    listed=status in ("listed", "on_sale") or price is not None,
                    price_ton=price,
                    listing_id=_as_str(pick(raw, "listing_id", "id")),
                )
            )
        return owned

    async def buy(self, listing: Listing) -> str:
        payload = await self.request(
            "buy",
            json_body={"nft_id": listing.listing_id, "price": listing.price_ton},
        )
        raw = payload if isinstance(payload, dict) else {}
        return str(pick(raw, "id", "tx_id", "order_id", default=listing.listing_id))

    async def list_for_sale(self, gift_external_id: str, price_ton: float) -> str:
        payload = await self.request(
            "sell",
            json_body={"nft_id": gift_external_id, "price": round(price_ton, 4)},
        )
        raw = payload if isinstance(payload, dict) else {}
        return str(pick(raw, "listing_id", "id", default=gift_external_id))

    async def change_price(self, listing_id: str, price_ton: float) -> None:
        await self.request(
            "change_price",
            json_body={"nft_id": listing_id, "price": round(price_ton, 4)},
        )

    async def delist(self, listing_id: str) -> None:
        await self.request("delist", json_body={"nft_id": listing_id})


def _iter_named(group: object):
    """Группа атрибутов приходит либо словарём, либо списком объектов."""
    if isinstance(group, dict):
        yield from group.items()
    elif isinstance(group, list):
        for item in group:
            if isinstance(item, dict):
                name = pick(item, "name", "value", "title")
                if name:
                    yield name, item


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


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _as_str(pick(value, "id", "name", "address"))
    return str(value)
