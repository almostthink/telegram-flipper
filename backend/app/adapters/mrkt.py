"""Адаптер MRKT.

ВНИМАНИЕ: у MRKT нет ни официального API, ни публично описанной схемы.
Пути ниже — обоснованное предположение по структуре мини-аппа, и их
почти наверняка придётся поправить по факту.

Чинится без пересборки exe: Настройки → импорт HAR. Вы делаете нужное
действие в MRKT в браузере, сохраняете HAR, приложение достаёт из него
реальные пути и заголовки и записывает в конфиг. См. app/adapters/har.py.
"""

from __future__ import annotations

import logging

from app.adapters.base import EndpointSpec, MarketEndpoints, Marketplace
from app.adapters.parsing import as_list, parse_gift, pick, to_datetime, to_ton
from app.domain import (
    ActivityEvent,
    ActivityKind,
    Balance,
    CollectionFloor,
    CollectionOffer,
    Listing,
    Market,
    OwnedGift,
)

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = MarketEndpoints(
    base_url="https://api.mrkt.xyz",
    endpoints={
        "collections": EndpointSpec("/v1/collections"),
        "listings": EndpointSpec("/v1/gifts"),
        "activity": EndpointSpec("/v1/activity"),
        "offers": EndpointSpec("/v1/offers"),
        "balance": EndpointSpec("/v1/user/balance"),
        "inventory": EndpointSpec("/v1/user/gifts"),
        "buy": EndpointSpec("/v1/gifts/buy", method="POST"),
        "sell": EndpointSpec("/v1/gifts/sell", method="POST"),
        "change_price": EndpointSpec("/v1/gifts/price", method="POST"),
        "delist": EndpointSpec("/v1/gifts/unlist", method="POST"),
    },
)

_ACTION_MAP = {
    "buy": ActivityKind.SALE,
    "sale": ActivityKind.SALE,
    "purchase": ActivityKind.SALE,
    "list": ActivityKind.LISTING,
    "listing": ActivityKind.LISTING,
    "price_change": ActivityKind.PRICE_UPDATE,
    "offer": ActivityKind.OFFER,
    "cancel": ActivityKind.DELIST,
}


class MrktAdapter(Marketplace):
    name = Market.MRKT
    supports_trading = True

    async def collection_floors(self) -> list[CollectionFloor]:
        payload = await self.request("collections", params={"limit": 500})
        floors: list[CollectionFloor] = []

        for raw in as_list(payload):
            name = pick(raw, "name", "title", "collection")
            floor = to_ton(pick(raw, "floor", "floor_price", "floorPrice", "min_price"))
            if not name or floor is None:
                continue
            floors.append(
                CollectionFloor(
                    market=self.name,
                    collection=str(name),
                    floor_ton=floor,
                    volume_24h_ton=to_ton(pick(raw, "volume_24h", "volume24h", "volume")),
                    sales_24h=_int(pick(raw, "sales_24h", "sales24h", "trades_24h")),
                    listed_count=_int(pick(raw, "listed", "listed_count", "on_sale")),
                )
            )
        return floors

    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        payload = await self.request(
            "listings",
            params={
                "collection": collection,
                "limit": min(limit, 200),
                "offset": 0,
                "sort": "price_asc",
                "status": "on_sale",
            },
        )

        listings: list[Listing] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "price_ton", "amount"))
            if price is None:
                continue
            gift = parse_gift(raw, fallback_collection=collection)
            listing_id = str(pick(raw, "id", "gift_id", "listing_id", default=gift.external_id))
            if not listing_id:
                continue
            listings.append(
                Listing(
                    market=self.name,
                    listing_id=listing_id,
                    gift=gift,
                    price_ton=price,
                    seller=_str(pick(raw, "seller", "owner", "user_id")),
                    listed_at=to_datetime(pick(raw, "listed_at", "created_at", "updated_at")),
                    url=f"https://t.me/mrkt?startapp={listing_id}",
                )
            )
        return listings

    async def activity(
        self, collection: str | None = None, *, limit: int = 100
    ) -> list[ActivityEvent]:
        params: dict[str, object] = {"limit": min(limit, 200)}
        if collection:
            params["collection"] = collection

        payload = await self.request("activity", params=params)
        events: list[ActivityEvent] = []

        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "amount"))
            happened = to_datetime(pick(raw, "created_at", "timestamp", "time", "date"))
            if price is None or happened is None:
                continue
            action = str(pick(raw, "type", "event", "action", default="")).lower()
            nft = raw.get("gift") if isinstance(raw.get("gift"), dict) else raw
            events.append(
                ActivityEvent(
                    market=self.name,
                    kind=_ACTION_MAP.get(action, ActivityKind.LISTING),
                    gift=parse_gift(nft, fallback_collection=collection or ""),
                    price_ton=price,
                    happened_at=happened,
                    external_id=_str(pick(raw, "id", "event_id")),
                    buyer=_str(pick(raw, "buyer", "to")),
                    seller=_str(pick(raw, "seller", "from")),
                )
            )
        return events

    async def collection_offers(self, collection: str) -> list[CollectionOffer]:
        payload = await self.request("offers", params={"collection": collection})
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
                    amount=_int(pick(raw, "amount", "count")) or 1,
                )
            )
        return offers

    # --- Торговля -------------------------------------------------------

    async def balance(self) -> Balance:
        payload = await self.request("balance")
        raw = payload if isinstance(payload, dict) else {}
        ton = to_ton(pick(raw, "ton", "balance", "available")) or 0.0
        return Balance(market=self.name, ton=ton)

    async def inventory(self) -> list[OwnedGift]:
        payload = await self.request("inventory", params={"limit": 200})
        owned: list[OwnedGift] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price", "amount"))
            status = str(pick(raw, "status", "state", default="")).lower()
            owned.append(
                OwnedGift(
                    market=self.name,
                    gift=parse_gift(raw),
                    listed=status in ("on_sale", "listed") or price is not None,
                    price_ton=price,
                    listing_id=_str(pick(raw, "listing_id", "id")),
                )
            )
        return owned

    async def buy(self, listing: Listing) -> str:
        payload = await self.request(
            "buy", json_body={"gift_id": listing.listing_id, "price": listing.price_ton}
        )
        raw = payload if isinstance(payload, dict) else {}
        return str(pick(raw, "id", "tx", "order_id", default=listing.listing_id))

    async def list_for_sale(self, gift_external_id: str, price_ton: float) -> str:
        payload = await self.request(
            "sell", json_body={"gift_id": gift_external_id, "price": round(price_ton, 4)}
        )
        raw = payload if isinstance(payload, dict) else {}
        return str(pick(raw, "listing_id", "id", default=gift_external_id))

    async def change_price(self, listing_id: str, price_ton: float) -> None:
        await self.request(
            "change_price", json_body={"gift_id": listing_id, "price": round(price_ton, 4)}
        )

    async def delist(self, listing_id: str) -> None:
        await self.request("delist", json_body={"gift_id": listing_id})


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _str(pick(value, "id", "name", "address"))
    return str(value)
