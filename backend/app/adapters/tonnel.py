"""Адаптер Tonnel (gifts2.tonnel.network).

Восстановлен из записи трафика и JS-бандла мини-аппа. Площадка устроена
иначе, чем MRKT и Portals, и три отличия определяют весь этот файл.

**Авторизация в теле, а не в заголовке.** Ни Authorization, ни Cookie
здесь нет вовсе — initData Telegram кладётся прямо в JSON запроса. Причём
под двумя разными именами: ``user_auth`` в ``/api/pageGifts`` и
``authData`` во всех остальных. Это не опечатка в записи, а факт: оба
варианта наблюдались в одном сеансе.

**Фильтры — запросы MongoDB.** Поле ``filter`` принимает строку с JSON
вида ``{"price": {"$exists": true}}``. Отсюда и берётся разделение:
обычные лоты ищутся по наличию цены, аукционные — по наличию
``auction_id``.

**Редкость зашита в имя атрибута.** Модель приходит строкой
``«Pyromancy (1.5%)»``, а не парой полей. Проценты, не промилле:
у остальных площадок редкость даётся в промилле, и путать их нельзя —
ошибка в десять раз прямо искажает оценку.

Аукцион есть только здесь. Шаг ставки процентный: каждая следующая
минимум на 5% выше текущей, что подтверждается кодом мини-аппа.
"""

from __future__ import annotations

import logging
import math
import re

from app.adapters.base import (
    AuthPlacement,
    EndpointSpec,
    MarketEndpoints,
    Marketplace,
    MarketplaceError,
)
from app.adapters.parsing import as_list, pick, to_datetime, to_ton
from app.domain import (
    ActivityEvent,
    ActivityKind,
    Attribute,
    AttributeKind,
    Auction,
    Balance,
    CollectionFloor,
    CollectionOffer,
    Gift,
    Listing,
    Market,
    MarketOrder,
)

log = logging.getLogger(__name__)

#: Во сколько раз следующая ставка должна превышать текущую. Подтверждено
#: кодом мини-аппа: minBid = ceil(highestBid * 1.05, 3).
BID_STEP_FACTOR = 1.05

#: До скольких знаков площадка округляет ставку — вверх, а не арифметически.
BID_DECIMALS = 3

DEFAULT_ENDPOINTS = MarketEndpoints(
    base_url="https://gifts2.tonnel.network",
    endpoints={
        # --- подтверждено записью трафика ---
        "listings": EndpointSpec("/api/pageGifts", method="POST"),
        "collections": EndpointSpec("/api/filterStats", method="POST", json_path="data"),
        "activity": EndpointSpec("/api/saleHistory", method="POST"),
        "balance": EndpointSpec("/api/balance/info", method="POST"),
        # --- пути из бандла мини-аппа, тела запросов не подтверждены ---
        "auctions": EndpointSpec("/api/pageGifts", method="POST"),
        "auction_bid": EndpointSpec("/api/auction/bid", method="POST"),
        "buy": EndpointSpec("/api/buyGift/", method="POST"),
        "sell": EndpointSpec("/api/listForSale", method="POST"),
        "change_price": EndpointSpec("/api/changePrice", method="POST"),
        "delist": EndpointSpec("/api/cancelSale", method="POST"),
        "my_orders": EndpointSpec("/api/buyOffer/getMyOffers", method="POST"),
        "orders": EndpointSpec("/api/buyOffer/getOffers", method="POST"),
        "order_create": EndpointSpec("/api/buyOffer/create", method="POST"),
        "order_cancel": EndpointSpec("/api/buyOffer/cancel", method="POST"),
    },
)

#: Фильтр обычных лотов. Подтверждён записью: цена есть, покупателя нет.
_SALE_FILTER = {"price": {"$exists": True}, "buyer": {"$exists": False}, "asset": "TON"}
#: Фильтр аукционов. Подтверждён кодом мини-аппа.
_AUCTION_FILTER = {"auction_id": {"$exists": True}, "status": "active", "asset": "TON"}

_RARITY = re.compile(r"^(?P<name>.*?)\s*\((?P<percent>[\d.]+)%\)\s*$")


def parse_attribute(raw: object, kind: AttributeKind) -> Attribute | None:
    """Разбор строки вида «Pyromancy (1.5%)».

    Проценты переводим в промилле: остальные площадки отдают редкость
    именно в них, и хранить два масштаба в одном поле — верный способ
    ошибиться в оценке ровно в десять раз.
    """
    if not raw or not isinstance(raw, str):
        return None

    match = _RARITY.match(raw.strip())
    if match is None:
        return Attribute(kind=kind, name=raw.strip())

    try:
        permille = float(match.group("percent")) * 10.0
    except ValueError:
        permille = None
    return Attribute(kind=kind, name=match.group("name").strip(), rarity_permille=permille)


def parse_tonnel_gift(raw: dict) -> Gift:
    number = pick(raw, "gift_num", "num")
    try:
        number_value = int(number) if number is not None else None
    except (TypeError, ValueError):
        number_value = None

    backdrop_data = raw.get("backdropData")
    center = None
    if isinstance(backdrop_data, dict):
        center = pick(backdrop_data, "centerColor")
    try:
        backdrop_color = int(center) if center is not None else None
    except (TypeError, ValueError):
        backdrop_color = None

    return Gift(
        collection=str(pick(raw, "name", "gift_name", default="")),
        external_id=str(pick(raw, "gift_id", default="")),
        number=number_value,
        backdrop_color=backdrop_color,
        model=parse_attribute(pick(raw, "model"), AttributeKind.MODEL),
        backdrop=parse_attribute(pick(raw, "backdrop"), AttributeKind.BACKDROP),
        symbol=parse_attribute(pick(raw, "symbol"), AttributeKind.SYMBOL),
    )


def min_next_bid(highest_ton: float, starting_ton: float, bids: int) -> float:
    """Минимальная допустимая ставка.

    До первой ставки годится стартовая цена, дальше — плюс пять
    процентов, округляя вверх. Округление именно вверх: арифметическое
    дало бы значение чуть ниже требуемого, и площадка отвергла бы ставку.
    """
    if bids <= 0:
        return starting_ton
    step = 10**BID_DECIMALS
    return math.ceil(highest_ton * BID_STEP_FACTOR * step) / step


class TonnelAdapter(Marketplace):
    name = Market.TONNEL
    supports_trading = True
    #: Учётные данные уходят в теле запроса, а не заголовком.
    auth_placement = AuthPlacement.BODY
    #: Выдача лотов ждёт их под именем user_auth, остальные — authData.
    #: Оба варианта наблюдались в одном сеансе, это не опечатка записи.
    body_auth_field = "authData"
    body_auth_overrides = {"listings": "user_auth", "auctions": "user_auth"}

    # --- Чтение рынка ----------------------------------------------------

    async def collection_floors(self) -> list[CollectionFloor]:
        """Флоры из filterStats.

        Площадка отдаёт их по паре «коллекция_модель», а не по коллекции:
        ключ ``«Jolly Chimp_Snow Yeti»``. Флор коллекции — минимум по её
        моделям, число лотов — сумма.
        """
        payload = await self.request("collections", json_body={})
        data = payload if isinstance(payload, dict) else {}

        best: dict[str, float] = {}
        counts: dict[str, int] = {}
        for key, stats in data.items():
            if not isinstance(stats, dict) or "_" not in key:
                continue
            collection = key.split("_", 1)[0].strip()
            floor = to_ton(pick(stats, "floorPrice"))
            if not collection or floor is None:
                continue
            if collection not in best or floor < best[collection]:
                best[collection] = floor
            counts[collection] = counts.get(collection, 0) + int(
                pick(stats, "howMany", default=0) or 0
            )

        return [
            CollectionFloor(
                market=self.name,
                collection=collection,
                floor_ton=floor,
                listed_count=counts.get(collection),
            )
            for collection, floor in best.items()
        ]

    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        rows = await self._page_gifts(_SALE_FILTER, collection=collection, limit=limit)
        listings: list[Listing] = []
        for raw in rows:
            listing = self._to_listing(raw)
            if listing is not None:
                listings.append(listing)
        return listings[:limit]

    async def latest_listings(self, *, limit: int = 20) -> list[Listing]:
        """Свежие лоты по всему рынку — сортировка по времени публикации."""
        rows = await self._page_gifts(_SALE_FILTER, limit=limit)
        return [item for raw in rows if (item := self._to_listing(raw)) is not None][:limit]

    async def auctions(self, *, limit: int = 50) -> list[Auction]:
        """Активные аукционы, ближайшие к завершению.

        Аукцион есть только у Tonnel, и работать с ним надо иначе, чем с
        книгой: цена растёт скачками по пять процентов, поэтому смысл
        имеют лишь торги, где следующая допустимая ставка ещё оставляет
        запас до флора.
        """
        rows = await self._page_gifts(
            _AUCTION_FILTER, limit=limit, sort={"auctionEndTime": 1, "gift_id": -1}
        )

        result: list[Auction] = []
        for raw in rows:
            data = raw.get("auction")
            if not isinstance(data, dict):
                continue

            history = data.get("bidHistory")
            history = history if isinstance(history, list) else []
            starting = to_ton(pick(data, "startingBid")) or 0.0
            highest = starting
            if history:
                highest = to_ton(pick(history[-1], "amount")) or starting

            result.append(
                Auction(
                    market=self.name,
                    auction_id=str(pick(data, "auction_id", default="")),
                    gift=parse_tonnel_gift(raw),
                    listing_id=str(pick(raw, "gift_id", default="")),
                    starting_bid_ton=starting,
                    highest_bid_ton=highest,
                    min_bid_ton=min_next_bid(highest, starting, len(history)),
                    ends_at=to_datetime(pick(data, "auctionEndTime")),
                    bids=len(history),
                )
            )
        return result

    async def activity(
        self, collection: str | None = None, *, limit: int = 100
    ) -> list[ActivityEvent]:
        """Лента сделок. Подтверждена записью: массив в корне ответа."""
        body: dict[str, object] = {
            "page": 1,
            "limit": min(limit, 50),
            "type": "ALL",
            "filter": {"gift_name": collection} if collection else {},
            "sort": {"timestamp": -1, "gift_id": -1},
        }
        payload = await self.request("activity", json_body=body)

        events: list[ActivityEvent] = []
        for raw in as_list(payload):
            price = to_ton(pick(raw, "price"))
            happened = to_datetime(pick(raw, "timestamp"))
            if price is None or happened is None:
                continue
            events.append(
                ActivityEvent(
                    market=self.name,
                    kind=ActivityKind.SALE,
                    gift=parse_tonnel_gift(raw),
                    price_ton=price,
                    happened_at=happened,
                    external_id=f"{pick(raw, 'gift_id')}:{pick(raw, 'timestamp')}",
                )
            )
        return events

    async def balance(self) -> Balance:
        payload = await self.request("balance", json_body={"ref": ""})
        raw = payload if isinstance(payload, dict) else {}
        return Balance(market=self.name, ton=float(pick(raw, "balance", default=0) or 0))

    # --- Заявки на покупку ------------------------------------------------

    async def my_orders(self) -> list[MarketOrder]:
        payload = await self.request("my_orders", json_body={"page": 1, "limit": 100})
        orders: list[MarketOrder] = []
        for raw in as_list(payload):
            order_id = pick(raw, "_id", "id", "offer_id")
            collection = pick(raw, "gift_name", "name", "collection")
            price = to_ton(pick(raw, "price", "amount"))
            if not order_id or not collection or price is None:
                continue
            orders.append(
                MarketOrder(
                    market=self.name,
                    order_id=str(order_id),
                    collection=str(collection),
                    price_ton=price,
                    amount=int(pick(raw, "amount", "count", default=1) or 1),
                )
            )
        return orders

    async def top_offers(self) -> list[CollectionOffer]:
        payload = await self.request("orders", json_body={"page": 1, "limit": 100})
        offers: list[CollectionOffer] = []
        for raw in as_list(payload):
            collection = pick(raw, "gift_name", "name", "collection")
            price = to_ton(pick(raw, "price", "amount"))
            if not collection or price is None:
                continue
            offers.append(
                CollectionOffer(
                    market=self.name, collection=str(collection), price_ton=price
                )
            )
        return offers

    async def create_order(self, collection: str, price_ton: float, amount: int) -> str:
        """Заявка на покупку. Тело не подтверждено — правится импортом HAR."""
        payload = await self.request(
            "order_create",
            json_body={"gift_name": collection, "price": price_ton, "amount": amount},
        )
        raw = payload if isinstance(payload, dict) else {}
        order_id = pick(raw, "_id", "id", "offer_id")
        if not order_id:
            raise MarketplaceError(f"Tonnel не принял заявку по «{collection}»")
        return str(order_id)

    async def cancel_order(self, order_id: str) -> None:
        await self.request("order_cancel", json_body={"offer_id": order_id})

    async def place_bid(self, auction_id: str, price_ton: float) -> None:
        """Ставка на аукционе. Тело не подтверждено — правится импортом HAR."""
        await self.request(
            "auction_bid", json_body={"auction_id": auction_id, "amount": price_ton}
        )

    # --- Служебное --------------------------------------------------------

    async def _page_gifts(
        self,
        base_filter: dict,
        *,
        collection: str | None = None,
        limit: int = 30,
        sort: dict | None = None,
    ) -> list[dict]:
        """Обход выдачи лотов. Фильтр — запрос в стиле MongoDB, строкой."""
        query = dict(base_filter)
        if collection:
            query["gift_name"] = collection

        body = {
            "page": 1,
            "limit": min(limit, 30),
            "sort": _as_json(sort or {"message_post_time": -1, "gift_id": -1}),
            "filter": _as_json(query),
            "ref": 0,
            "price_range": None,
        }
        payload = await self.request("listings", json_body=body)
        return as_list(payload)

    def _to_listing(self, raw: dict) -> Listing | None:
        if pick(raw, "status") not in (None, "forsale"):
            return None
        price = to_ton(pick(raw, "price"))
        if price is None:
            return None

        gift = parse_tonnel_gift(raw)
        if not gift.external_id:
            return None

        return Listing(
            market=self.name,
            listing_id=gift.external_id,
            gift=gift,
            price_ton=price,
            listed_at=to_datetime(pick(raw, "message_post_time")),
            url=f"https://t.me/tonnel_network_bot/gifts?startapp={gift.external_id}",
            # Подарок нельзя передать до can_transfer_since — для нас это
            # то же самое, что блокировка перепродажи.
            resale_available_at=to_datetime(pick(raw, "can_transfer_since")),
        )


def _as_json(value: dict) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
