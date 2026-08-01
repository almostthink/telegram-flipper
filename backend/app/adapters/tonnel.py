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

#: Торговля живёт на отдельном домене — не на том, откуда читаются лоты.
#: Площадка держит две площадки серверов, российскую и общую, и выбирает
#: их по флагу в localStorage. Записанный сеанс шёл через российскую, её и
#: берём по умолчанию; общая остаётся запасным вариантом на случай, если
#: первая недоступна.
TRADE_HOST = "https://rs-api.tonnel.network"
TRADE_HOST_GLOBAL = "https://gifts.coffin.meme"

#: Актив сделки. Площадка умеет и другие, но подарки за них торгуются
#: отдельным рынком — смешивать их в одной книге нельзя.
ASSET = "TON"

#: Ключ подписи из кода мини-аппа. Секретом он не является — лежит в
#: бандле открытым текстом, — но без него площадка отвергает покупку и
#: выставление: поле ``wtf`` проверяется на сервере.
SIGNING_KEY = "yowtfisthispieceofshitiiit"


def sign_timestamp(timestamp: int) -> str:
    """Поле ``wtf``: отметка времени, зашифрованная как в мини-аппе."""
    from app.adapters.cryptojs import encrypt

    return encrypt(str(timestamp), SIGNING_KEY)


def _trade(path: str) -> EndpointSpec:
    return EndpointSpec(path, method="POST", base_url=TRADE_HOST)


DEFAULT_ENDPOINTS = MarketEndpoints(
    base_url="https://gifts2.tonnel.network",
    endpoints={
        # --- чтение: подтверждено записью трафика ---
        "listings": EndpointSpec("/api/pageGifts", method="POST"),
        "collections": EndpointSpec("/api/filterStats", method="POST", json_path="data"),
        "activity": EndpointSpec("/api/saleHistory", method="POST"),
        "balance": EndpointSpec("/api/balance/info", method="POST"),
        "auctions": EndpointSpec("/api/pageGifts", method="POST"),
        # Состояние одного лота вместе с историей ставок — по нему видно,
        # перебили нашу ставку или нет.
        "gift_data": EndpointSpec("/api/giftData/{gift_id}", method="POST"),
        # --- торговля: отдельный домен ---
        # Ставка подтверждена записью целиком, вместе с ответом.
        "auction_bid": _trade("/api/auction/bid"),
        "auction_create": _trade("/api/auction/create"),
        "auction_cancel": _trade("/api/auction/cancel"),
        # Остальные тела восстановлены из кода мини-аппа: там они собраны
        # явно, поле в поле. Живой записью пока не подтверждены.
        "buy": _trade("/api/buyGift/{sale_id}"),
        "sell": _trade("/api/listForSale"),
        "change_price": _trade("/api/changePrice"),
        "delist": _trade("/api/cancelSale"),
        "my_orders": _trade("/api/buyOffer/getMyOffers"),
        "orders": _trade("/api/buyOffer/getOffers"),
        "order_create": _trade("/api/buyOffer/create"),
        "order_cancel": _trade("/api/buyOffer/cancel"),
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
        payload = await self.request(
            "my_orders", json_body={"pageSize": 100, "filter": {}}
        )
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

    async def offers_for(self, gift_id: str) -> list[CollectionOffer]:
        """Предложения по одному лоту.

        Книги заявок по коллекции у Tonnel нет: предложение адресуется
        конкретному экземпляру, а не «любому Evil Eye». Поэтому спросить
        «почём сейчас берут коллекцию» здесь попросту негде.
        """
        payload = await self.request("orders", json_body={"gift_id": _as_int(gift_id)})
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

    async def top_offers(self) -> list[CollectionOffer]:
        """Пусто, и это не заглушка, а свойство площадки — см. offers_for."""
        return []

    async def create_order(self, collection: str, price_ton: float, amount: int) -> str:
        """Предложение цены по конкретному лоту.

        У Tonnel заявка адресная: не «куплю любой из коллекции», как на
        MRKT, а предложение владельцу этого экземпляра. Поэтому сюда
        приходит идентификатор лота, а не имя коллекции, и ``amount``
        площадкой не поддерживается — предложение всегда на один подарок.
        """
        payload = await self.request(
            "order_create",
            json_body={
                "gift_id": _as_int(collection),
                "amount": price_ton,
                "asset": ASSET,
            },
        )
        raw = payload if isinstance(payload, dict) else {}
        order_id = pick(raw, "offer_id", "_id", "id")
        if not order_id:
            raise MarketplaceError(f"Tonnel не принял предложение по лоту {collection}")
        return str(order_id)

    async def cancel_order(self, order_id: str) -> None:
        await self.request("order_cancel", json_body={"offer_id": order_id})

    async def place_bid(self, auction_id: str, price_ton: float) -> None:
        """Ставка на аукционе. Подтверждена записью вместе с ответом.

        Сумма уходит строкой — так её отправляет мини-апп, и так она
        доходит без потерь: 3.045 в виде числа с плавающей точкой
        превращается в 3.0449999999999999, и площадка вправе счесть такую
        ставку ниже минимальной.
        """
        self._require_success(
            await self.request(
                "auction_bid",
                json_body={
                    "auction_id": auction_id,
                    "amount": _as_amount(price_ton),
                    "asset": ASSET,
                },
            ),
            f"ставка {price_ton:.3f} TON по аукциону {auction_id}",
        )

    async def cancel_auction(self, auction_id: str) -> None:
        self._require_success(
            await self.request("auction_cancel", json_body={"auction_id": auction_id}),
            f"снятие аукциона {auction_id}",
        )

    # --- Покупка и продажа ------------------------------------------------

    async def buy(self, listing: Listing) -> str:
        """Покупка лота.

        Тело восстановлено из кода мини-аппа: к цене и активу добавляются
        отметка времени и её подпись. Без подписи площадка отказывает.
        """
        stamp = _now()
        payload = await self.request(
            "buy",
            path_params={"sale_id": listing.listing_id},
            json_body={
                "asset": ASSET,
                "price": listing.price_ton,
                "timestamp": stamp,
                "wtf": sign_timestamp(stamp),
            },
        )
        self._require_success(payload, f"покупка лота {listing.listing_id}")
        return listing.listing_id

    async def list_for_sale(self, gift_external_id: str, price_ton: float) -> str:
        stamp = _now()
        payload = await self.request(
            "sell",
            json_body={
                "gift_id": _as_int(gift_external_id),
                "price": price_ton,
                "asset": ASSET,
                "timestamp": stamp,
                "wtf": sign_timestamp(stamp),
            },
        )
        self._require_success(payload, f"выставление лота {gift_external_id}")
        return gift_external_id

    async def change_price(self, listing_id: str, price_ton: float) -> None:
        """Смена цены. Цена уходит строкой — так собирает тело мини-апп."""
        self._require_success(
            await self.request(
                "change_price",
                json_body={"sale_id": _as_int(listing_id), "price": _as_amount(price_ton)},
            ),
            f"смена цены лота {listing_id}",
        )

    async def delist(self, listing_id: str) -> None:
        self._require_success(
            await self.request("delist", json_body={"gift_id": _as_int(listing_id)}),
            f"снятие лота {listing_id} с продажи",
        )

    async def gift_state(self, gift_id: str) -> dict:
        """Состояние лота вместе с историей ставок.

        Нужно перед тем, как перебивать: между проходами сканера ставку
        могли поднять, и слепая ставка по устаревшей цене будет отвергнута.
        """
        payload = await self.request(
            "gift_data", path_params={"gift_id": gift_id}, json_body={"ref": ""}
        )
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _require_success(payload: object, what: str) -> None:
        """Площадка отвечает 200 и на отказ — приговор лежит в теле.

        Без этой проверки неудачная покупка выглядела бы удачной, и
        приложение завело бы позицию на подарок, которого у него нет.
        """
        if not isinstance(payload, dict):
            return
        status = str(payload.get("status", "")).lower()
        if status and status != "success":
            reason = payload.get("message") or status
            raise MarketplaceError(f"Tonnel отклонил {what}: {reason}")

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


def _now() -> int:
    import time

    return int(time.time())


def _as_int(value: object) -> int | str:
    """Идентификаторы лотов у Tonnel числовые, но приходят к нам строками."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return str(value)


def _as_amount(price_ton: float) -> str:
    """Сумма строкой, без хвоста двоичного представления.

    3.045 в виде float печатается как 3.0449999999999999, и площадка
    вправе счесть такую ставку ниже минимальной. Мини-апп отправляет
    ровно то, что видит человек, — делаем так же.
    """
    return f"{round(price_ton, BID_DECIMALS):g}"
