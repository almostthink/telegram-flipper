"""Адаптер MRKT (api.tgmrkt.io).

Пути и имена полей восстановлены из реальной записи трафика мини-аппа,
а не угаданы. Проверенное по факту помечено «подтверждено», остальное —
«не подтверждено», и это видно прямо в коде.

Две особенности площадки, которые определяют всю экономику:

**Комиссия 2%, и платит её покупатель.** В ответе у каждого лота есть
``salePrice`` и ``salePriceWithoutFee``, их отношение ровно 1.02 на всех
наблюдавшихся лотах. Продавец получает свою цену целиком, надбавку
добавляют сверху покупателю.

Отсюда соглашение всего приложения: **везде хранится цена покупателя**.
Она сравнима между площадками и равна тому, что мы реально заплатим.
Чтобы наш лот показывался покупателю за Y, выставлять его надо за
Y / 1.02 — этот пересчёт делает сам адаптер, наружу он не торчит.

**Часть лотов заблокирована для перепродажи.** Поле ``nextResaleDate``
может стоять в будущем: подарок куплен, но продать его нельзя ещё
несколько дней. Для флиппера это худший исход — деньги заморожены, а
модель об этом не знает. Дата передаётся дальше и проверяется в отборе.
"""

from __future__ import annotations

import logging

from app.adapters.base import (
    EndpointSpec,
    MarketEndpoints,
    Marketplace,
    MarketplaceError,
    dig,
)
from app.adapters.parsing import as_list, nano_to_ton, pick, to_datetime
from app.domain import (
    ActivityEvent,
    ActivityKind,
    Attribute,
    AttributeFloor,
    AttributeKind,
    Balance,
    CollectionFloor,
    Gift,
    Listing,
    Market,
    OwnedGift,
)

log = logging.getLogger(__name__)

#: Во столько раз цена для покупателя выше цены продавца. Подтверждено:
#: salePrice / salePriceWithoutFee = 1.020000 на всех наблюдавшихся лотах.
BUYER_MARKUP = 1.02

#: Комиссия в терминах приложения. Цены хранятся покупательские, поэтому
#: издержка выражается как удержание с продажи: получить Y/1.02 от Y — это
#: то же самое, что отдать 1.961% с покупательской цены.
FEE_AS_SELL_SIDE = 1 - 1 / BUYER_MARKUP  # ≈ 0.019608

DEFAULT_ENDPOINTS = MarketEndpoints(
    base_url="https://api.tgmrkt.io",
    endpoints={
        # --- подтверждено записью трафика ---
        "listings": EndpointSpec("/api/v1/gifts/saling", method="POST", json_path="gifts"),
        "collections": EndpointSpec("/api/v1/gifts/collections"),
        "backdrops": EndpointSpec("/api/v1/gifts/backdrops", method="POST"),
        "symbols": EndpointSpec("/api/v1/gifts/symbols", method="POST"),
        "balance": EndpointSpec("/api/v1/balance"),
        "me": EndpointSpec("/api/v1/me"),
        "auth": EndpointSpec("/api/v1/auth", method="POST"),
        # Лента событий рынка. Путь неочевидный — не /activities/, как
        # напрашивалось по счётчику уведомлений, а /feed.
        "activity": EndpointSpec("/api/v1/feed", method="POST", json_path="items"),
        # --- не подтверждено: в записи этих действий не было ---
        # Предположения по симметрии с backdrops/symbols.
        # Правятся импортом HAR без пересборки.
        "models": EndpointSpec("/api/v1/gifts/models", method="POST"),
        "inventory": EndpointSpec("/api/v1/gifts/my", method="POST", json_path="gifts"),
        "buy": EndpointSpec("/api/v1/gifts/buy", method="POST"),
        "sell": EndpointSpec("/api/v1/gifts/sell", method="POST"),
        "change_price": EndpointSpec("/api/v1/gifts/price", method="POST"),
        "delist": EndpointSpec("/api/v1/gifts/unlist", method="POST"),
    },
)

#: Тело запроса списка лотов. Поля подтверждены записью; неиспользуемые
#: фильтры оставлены как None — сервер ожидает их присутствия.
_LISTING_QUERY: dict[str, object] = {
    "count": 20,
    "cursor": "",
    "collectionNames": [],
    "modelNames": [],
    "backdropNames": [],
    "symbolNames": [],
    "minPrice": None,
    "maxPrice": None,
    "number": None,
    "isPremarket": None,
    "isNew": None,
    "luckyBuy": None,
    "giftType": None,
    "craftable": None,
    "isCrafted": None,
    "tgCanBeCraftedFrom": None,
    "removeSelfSales": None,
    "isTransferable": None,
    "availableForStaking": None,
    "forGame": None,
    "ordering": "Price",
    "lowToHigh": True,
    "query": None,
}

#: Тело запроса ленты событий. Подтверждено записью трафика раздела
#: истории: обход тот же курсорный, что и у списка лотов.
#:
#: ``type`` берём парой: продажа даёт цену сделки, листинг — момент, с
#: которого лот стоял в книге. Без второго не восстановить время до
#: продажи, а оно составляет 30% балла ликвидности.
_FEED_QUERY: dict[str, object] = {
    "count": 20,
    "cursor": "",
    "collectionNames": [],
    "modelNames": [],
    "backdropNames": [],
    "number": None,
    "type": ["Sale", "Listing"],
    "minPrice": None,
    "maxPrice": None,
    "ordering": "Latest",
    "lowToHigh": False,
    "query": None,
}

_ACTION_MAP = {
    "buy": ActivityKind.SALE,
    "sale": ActivityKind.SALE,
    "sold": ActivityKind.SALE,
    "purchase": ActivityKind.SALE,
    "list": ActivityKind.LISTING,
    "listing": ActivityKind.LISTING,
    "saling": ActivityKind.LISTING,
    "price_change": ActivityKind.PRICE_UPDATE,
    "offer": ActivityKind.OFFER,
    "cancel": ActivityKind.DELIST,
}

#: Страниц за один обход коллекции. 20 лотов на страницу — размер,
#: который использует сам мини-апп.
MAX_PAGES = 6

#: Страниц ленты событий за один обход. Лента идёт от свежих к старым,
#: и для суточной скорости продаж хватает нескольких страниц.
MAX_FEED_PAGES = 8


def parse_mrkt_gift(raw: dict) -> Gift:
    """Разбор подарка. Имена полей подтверждены записью трафика."""

    def attribute(name_key: str, rarity_key: str, kind: AttributeKind) -> Attribute | None:
        name = pick(raw, name_key)
        if not name:
            return None
        rarity = pick(raw, rarity_key)
        try:
            rarity_value = float(rarity) if rarity is not None else None
        except (TypeError, ValueError):
            rarity_value = None
        return Attribute(kind=kind, name=str(name), rarity_permille=rarity_value)

    number = pick(raw, "number")
    try:
        number_value = int(number) if number is not None else None
    except (TypeError, ValueError):
        number_value = None

    return Gift(
        collection=str(pick(raw, "collectionName", "collectionTitle", "title", default="")),
        external_id=str(pick(raw, "id", default="")),
        number=number_value,
        model=attribute("modelName", "modelRarityPerMille", AttributeKind.MODEL),
        backdrop=attribute("backdropName", "backdropRarityPerMille", AttributeKind.BACKDROP),
        symbol=attribute("symbolName", "symbolRarityPerMille", AttributeKind.SYMBOL),
    )


class MrktAdapter(Marketplace):
    name = Market.MRKT
    supports_trading = True

    async def exchange_init_data(self, init_data: str, *, photo: str = "") -> str:
        """Меняем initData Telegram на собственный токен площадки.

        MRKT не принимает initData напрямую, в отличие от Portals. Сначала
        нужен обмен: POST /api/v1/auth с телом {data, photo, appId} отдаёт
        токен, который дальше ставится в Authorization **без префикса** —
        по записи трафика это UUID из 36 символов.

        Тело запроса подтверждено записью, включая appId=null.
        """
        payload = await self.request(
            "auth", json_body={"data": init_data, "photo": photo, "appId": None}
        )
        raw = payload if isinstance(payload, dict) else {}
        token = pick(raw, "token")
        if not token:
            raise MarketplaceError("MRKT не вернул токен в ответе на авторизацию")
        return str(token)

    async def collection_floors(self) -> list[CollectionFloor]:
        """Флоры коллекций. Подтверждено: массив в корне ответа."""
        payload = await self.request("collections")
        floors: list[CollectionFloor] = []

        for raw in as_list(payload):
            name = pick(raw, "name", "title")
            floor = nano_to_ton(pick(raw, "floorPriceNanoTons"))
            if not name or floor is None:
                continue
            floors.append(
                CollectionFloor(
                    market=self.name,
                    collection=str(name),
                    floor_ton=floor,
                    volume_24h_ton=nano_to_ton(pick(raw, "volume")),
                    listed_count=None,
                )
            )
        return floors

    async def previous_day_floors(self) -> dict[str, float]:
        """Вчерашние флоры — площадка отдаёт их вместе с текущими.

        Готовый тренд без накопления собственной истории: полезно на
        первом запуске, когда своих замеров ещё нет.
        """
        payload = await self.request("collections")
        result: dict[str, float] = {}
        for raw in as_list(payload):
            name = pick(raw, "name", "title")
            previous = nano_to_ton(pick(raw, "previousDayFloorPriceNanoTons"))
            if name and previous is not None:
                result[str(name)] = previous
        return result

    async def _pages(
        self, endpoint: str, query: dict[str, object], *, limit: int, max_pages: int
    ):
        """Постраничный обход по курсору.

        Ответ берём целиком (``raw=True``): курсор следующей страницы лежит
        рядом с массивом, и выборка по ``json_path`` его отбрасывала — обход
        молча заканчивался на первой странице.
        """
        spec = self.endpoints.get(endpoint)
        cursor = ""
        seen = 0

        for _ in range(max_pages):
            payload = await self.request(
                endpoint, json_body={**query, "count": min(limit, 20), "cursor": cursor}, raw=True
            )
            # json_path указывает на массив, но после правки путей через HAR
            # он может отсутствовать — тогда ищем массив по имени ключа.
            rows = as_list(dig(payload, spec.json_path)) or as_list(payload)
            if not rows:
                return

            yield rows
            seen += len(rows)
            if seen >= limit:
                return

            cursor = str(pick(payload, "cursor", default="")) if isinstance(payload, dict) else ""
            if not cursor:
                return

    async def listings(self, collection: str, *, limit: int = 100) -> list[Listing]:
        """Лоты в продаже, от дешёвых к дорогим. Постраничный обход по курсору."""
        collected: list[Listing] = []
        query = {**_LISTING_QUERY, "collectionNames": [collection] if collection else []}

        async for rows in self._pages("listings", query, limit=limit, max_pages=MAX_PAGES):
            for raw in rows:
                listing = self._to_listing(raw, collection)
                if listing is not None:
                    collected.append(listing)
            if len(collected) >= limit:
                break

        return collected[:limit]

    def _to_listing(self, raw: dict, collection: str) -> Listing | None:
        # Аукционные лоты и уже снятые с продажи в флиппинг не годятся.
        if pick(raw, "isOnAuction") is True or pick(raw, "isOnSale") is False:
            return None
        # Свои же лоты покупать бессмысленно.
        if pick(raw, "isMine") is True:
            return None

        price = nano_to_ton(pick(raw, "salePrice"))
        if price is None:
            return None

        gift = parse_mrkt_gift(raw)
        if not gift.collection:
            gift.collection = collection
        if not gift.external_id:
            return None

        return Listing(
            market=self.name,
            listing_id=gift.external_id,
            gift=gift,
            price_ton=price,
            listed_at=to_datetime(pick(raw, "receivedDate")),
            url=f"https://t.me/mrkt/app?startapp={gift.external_id}",
            # Момент, начиная с которого лот вообще можно перепродать.
            resale_available_at=to_datetime(
                pick(raw, "nextResaleDate", "unlockDate", "returnLockedUntil")
            ),
            locked=bool(pick(raw, "isLocked") or pick(raw, "isLockedForSale")),
        )

    async def attribute_floors(self, collection: str) -> list[AttributeFloor]:
        """Флоры по фонам, символам и моделям.

        У MRKT это три отдельных эндпоинта, а не один. Модели не были в
        записи трафика — если путь не угадан, запрос молча пропускается,
        остальные два всё равно отработают.
        """
        result: list[AttributeFloor] = []
        groups = (
            ("backdrops", AttributeKind.BACKDROP, "backdropName"),
            ("symbols", AttributeKind.SYMBOL, "symbolName"),
            ("models", AttributeKind.MODEL, "modelName"),
        )

        for endpoint, kind, name_key in groups:
            try:
                payload = await self.request(
                    endpoint, json_body={"collections": [collection]}
                )
            except Exception as exc:  # noqa: BLE001 — один срез не должен ронять остальные
                log.debug("MRKT/%s: %s", endpoint, exc)
                continue

            for raw in as_list(payload):
                name = pick(raw, name_key, "name")
                floor = nano_to_ton(pick(raw, "floorNanoTons", "floorPriceNanoTons"))
                if not name or floor is None:
                    continue
                result.append(
                    AttributeFloor(
                        market=self.name,
                        collection=collection,
                        kind=kind,
                        name=str(name),
                        floor_ton=floor,
                        rarity_permille=_as_float(pick(raw, "rarityPerMille")),
                    )
                )
        return result

    async def activity(
        self, collection: str | None = None, *, limit: int = 100
    ) -> list[ActivityEvent]:
        """Лента сделок: POST /api/v1/feed.

        Подтверждено записью трафика раздела истории. Элемент ленты —
        ``{type, id, amount, date, gift}``: тип события строкой («sale» или
        «listing»), сумма в нанотонах и подарок целиком, тем же объектом,
        что и в списке лотов.

        Это единственный источник истории сделок MRKT. Без него не считаются
        ни скорость продаж, ни время до продажи — 65% веса балла ликвидности,
        и отбор идёт вслепую.

        Сумма покупательская: у продажи ``amount`` совпадает с ``salePrice``
        подарка, то есть уже включает 2% надбавки. Пересчитывать не нужно —
        приложение везде хранит цену покупателя.

        Фильтр по коллекции передаём по симметрии со списком лотов — в
        записи лента была общей. Если площадка его проигнорирует, вреда нет:
        коллекция берётся из самого подарка, а не из запроса.
        """
        events: list[ActivityEvent] = []
        query = {**_FEED_QUERY, "collectionNames": [collection] if collection else []}

        async for rows in self._pages("activity", query, limit=limit, max_pages=MAX_FEED_PAGES):
            for raw in rows:
                event = self._to_event(raw, collection)
                if event is not None:
                    events.append(event)
            if len(events) >= limit:
                break

        return events[:limit]

    def _to_event(self, raw: dict, collection: str | None) -> ActivityEvent | None:
        price = nano_to_ton(pick(raw, "amount", "price", "salePrice"))
        happened = to_datetime(pick(raw, "date", "createdAt", "soldAt", "timestamp"))
        if price is None or happened is None:
            return None

        action = str(pick(raw, "type", "kind", "action", default="")).lower()
        kind = _ACTION_MAP.get(action)
        if kind is None:
            # Неизвестное событие лучше пропустить, чем записать продажей:
            # выдуманная сделка завышает и скорость продаж, и историю цен.
            log.debug("MRKT/feed: неизвестный тип события %r", action)
            return None

        nested = raw.get("gift") if isinstance(raw.get("gift"), dict) else raw
        gift = parse_mrkt_gift(nested)
        if not gift.collection and collection:
            gift.collection = collection

        return ActivityEvent(
            market=self.name,
            kind=kind,
            gift=gift,
            price_ton=price,
            happened_at=happened,
            external_id=_as_str(pick(raw, "id")),
            buyer=_as_str(pick(raw, "buyerId", "buyer", "to")),
            seller=_as_str(pick(raw, "sellerId", "seller", "from")),
        )

    # --- Торговля -------------------------------------------------------

    async def balance(self) -> Balance:
        """Баланс. Подтверждено поле totalHard в нанотонах."""
        payload = await self.request("balance")
        raw = payload if isinstance(payload, dict) else {}
        return Balance(market=self.name, ton=nano_to_ton(pick(raw, "totalHard")) or 0.0)

    async def inventory(self) -> list[OwnedGift]:
        payload = await self.request(
            "inventory", json_body={**_LISTING_QUERY, "count": 100, "cursor": ""}
        )
        owned: list[OwnedGift] = []
        for raw in as_list(payload):
            gift = parse_mrkt_gift(raw)
            price = nano_to_ton(pick(raw, "salePrice"))
            owned.append(
                OwnedGift(
                    market=self.name,
                    gift=gift,
                    listed=bool(pick(raw, "isOnSale")),
                    price_ton=price,
                    listing_id=gift.external_id or None,
                )
            )
        return owned

    async def buy(self, listing: Listing) -> str:
        """Покупка. Цена передаётся покупательская, как её отдаёт площадка."""
        payload = await self.request(
            "buy",
            json_body={
                "id": listing.listing_id,
                "price": _to_nano(listing.price_ton),
            },
        )
        raw = payload if isinstance(payload, dict) else {}
        return str(pick(raw, "id", "transactionId", default=listing.listing_id))

    async def list_for_sale(self, gift_external_id: str, price_ton: float) -> str:
        """Выставление на продажу.

        На вход приходит цена, которую увидит покупатель. Площадка ждёт
        цену продавца, поэтому делим на надбавку — иначе лот встанет в
        книгу на 2% дороже задуманного и не продастся.
        """
        payload = await self.request(
            "sell",
            json_body={
                "id": gift_external_id,
                "price": _to_nano(price_ton / BUYER_MARKUP),
            },
        )
        raw = payload if isinstance(payload, dict) else {}
        return str(pick(raw, "id", default=gift_external_id))

    async def change_price(self, listing_id: str, price_ton: float) -> None:
        await self.request(
            "change_price",
            json_body={"id": listing_id, "price": _to_nano(price_ton / BUYER_MARKUP)},
        )

    async def delist(self, listing_id: str) -> None:
        await self.request("delist", json_body={"id": listing_id})


def _to_nano(price_ton: float) -> int:
    return int(round(price_ton * 1_000_000_000))


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _as_str(pick(value, "id", "name"))
    return str(value)
