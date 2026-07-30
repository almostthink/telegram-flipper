"""Проверки адаптера MRKT на реальных ответах площадки.

Фикстура ``fixtures/mrkt_responses.json`` — это настоящие ответы
api.tgmrkt.io, снятые из записи трафика мини-аппа. Обезличены только
идентификаторы лотов; структура и имена полей подлинные.

Это единственные тесты в проекте, которые проверяют разбор против
фактической схемы API, а не против моих предположений о ней. Если MRKT
поменяет формат, упадут именно они.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.base import MarketEndpoints, MarketplaceError
from app.adapters.har import classify
from app.adapters.mrkt import (
    BUYER_MARKUP,
    DEFAULT_ENDPOINTS,
    FEE_AS_SELL_SIDE,
    MrktAdapter,
    parse_mrkt_gift,
)
from app.analytics import pricing
from app.domain import ActivityKind, AttributeKind, Gift, Listing, Market

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mrkt_responses.json").read_text(encoding="utf-8")
)


@pytest.fixture
def adapter() -> MrktAdapter:
    return MrktAdapter(endpoints=DEFAULT_ENDPOINTS, fee_sell=FEE_AS_SELL_SIDE)


class Pages(list):
    """Последовательность ответов эндпоинта — по одной на страницу."""


#: Ответ за последней страницей: пустой список и пустой курсор. Именно так
#: ведёт себя площадка, и фейк обязан это повторять — иначе постраничный
#: обход в тестах крутится по одной и той же странице.
EXHAUSTED = {"gifts": [], "items": [], "cursor": ""}


class FakeAdapter(MrktAdapter):
    """Адаптер с подменённым транспортом: сеть в тестах не нужна."""

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__(endpoints=DEFAULT_ENDPOINTS, fee_sell=FEE_AS_SELL_SIDE)
        self.responses = responses
        self.calls: list[tuple[str, dict | None, dict | None]] = []
        self._served: dict[str, int] = {}

    async def request(self, endpoint, *, params=None, json_body=None, raw=False):
        self.calls.append((endpoint, params, json_body))
        if endpoint not in self.responses:
            raise KeyError(endpoint)

        page = self._served.get(endpoint, 0)
        self._served[endpoint] = page + 1
        stored = self.responses[endpoint]
        pages = list(stored) if isinstance(stored, Pages) else [stored]
        payload = pages[page] if page < len(pages) else EXHAUSTED

        spec = self.endpoints.get(endpoint)
        from app.adapters.base import dig

        return payload if raw else dig(payload, spec.json_path)


# --- Разбор подарка ------------------------------------------------------


def test_parses_real_gift_fields():
    raw = FIXTURES["saling"]["gifts"][0]
    gift = parse_mrkt_gift(raw)

    assert gift.collection == raw["collectionName"]
    assert gift.external_id == raw["id"]
    assert gift.number == raw["number"]

    assert gift.model is not None
    assert gift.model.name == raw["modelName"]
    assert gift.model.rarity_permille == raw["modelRarityPerMille"]
    assert gift.model.kind is AttributeKind.MODEL

    assert gift.backdrop is not None
    assert gift.backdrop.name == raw["backdropName"]
    assert gift.backdrop.rarity_permille == raw["backdropRarityPerMille"]


def test_missing_symbol_rarity_does_not_break_parsing():
    """У части лотов symbolRarityPerMille равен null — это не ошибка."""
    raw = dict(FIXTURES["saling"]["gifts"][0], symbolRarityPerMille=None)
    gift = parse_mrkt_gift(raw)

    assert gift.symbol is not None
    assert gift.symbol.rarity_permille is None


# --- Комиссия ------------------------------------------------------------


def test_fee_matches_observed_ratio():
    """salePrice / salePriceWithoutFee = 1.02 на всех наблюдавшихся лотах."""
    for raw in FIXTURES["saling"]["gifts"]:
        assert raw["salePrice"] / raw["salePriceWithoutFee"] == pytest.approx(
            BUYER_MARKUP, abs=1e-9
        )


def test_breakeven_on_mrkt_is_two_percent():
    """Ключевое следствие: на MRKT безубыток наступает при наценке 2%.

    Если бы комиссия была 5% как у типичной площадки, порог был бы 5.3% —
    разница в два с половиной раза меняет, какие сделки вообще имеют смысл.
    """
    assert pricing.breakeven_markup(FEE_AS_SELL_SIDE) == pytest.approx(0.02, abs=1e-6)


def test_net_roi_zero_at_two_percent_markup():
    ask = 100.0
    roi = pricing.net_roi(ask, ask * 1.02, fee_sell=FEE_AS_SELL_SIDE, gas_ton=0.0)
    assert roi == pytest.approx(0.0, abs=1e-9)


# --- Разбор листингов ----------------------------------------------------


async def test_listings_parse_prices_from_nanotons():
    adapter = FakeAdapter({"listings": FIXTURES["saling"]})
    listings = await adapter.listings("Evil Eye", limit=20)

    assert listings, "лоты должны разобраться"
    first = listings[0]
    raw = FIXTURES["saling"]["gifts"][0]

    # Цена покупателя, а не продавца: именно её мы заплатим.
    assert first.price_ton == pytest.approx(raw["salePrice"] / 1e9)
    assert first.market is Market.MRKT


async def test_listings_exclude_auctions_own_and_locked_lots():
    adapter = FakeAdapter({"listings": FIXTURES["saling"]})
    listings = await adapter.listings("Evil Eye", limit=50)
    ids = {item.listing_id for item in listings}

    # Аукцион — не флип, свой лот покупать бессмысленно.
    assert "00000000-0000-4000-8000-000000000091" not in ids
    assert "00000000-0000-4000-8000-000000000092" not in ids

    # Заблокированный лот из выдачи не убираем, но помечаем: решение
    # принимает отбор сигналов, а данные должны остаться полными.
    locked = next(
        item for item in listings if item.listing_id == "00000000-0000-4000-8000-000000000090"
    )
    assert locked.resale_available_at is not None
    assert not locked.resalable_at(datetime(2026, 8, 1, tzinfo=UTC))


async def test_listing_request_body_matches_api_contract():
    """Тело запроса должно повторять то, что шлёт сам мини-апп."""
    adapter = FakeAdapter({"listings": FIXTURES["saling"]})
    await adapter.listings("Plush Pepe", limit=20)

    _, _, body = adapter.calls[0]
    assert body["collectionNames"] == ["Plush Pepe"]
    assert body["lowToHigh"] is True, "нужны сначала дешёвые лоты"
    # Сервер ожидает полный набор полей фильтра, даже пустых.
    for required in ("count", "cursor", "minPrice", "maxPrice", "ordering"):
        assert required in body


# --- Коллекции и атрибуты ------------------------------------------------


async def test_collection_floors_from_real_response():
    adapter = FakeAdapter({"collections": FIXTURES["collections"]})
    floors = await adapter.collection_floors()

    assert floors
    by_name = {item.collection: item for item in floors}
    for raw in FIXTURES["collections"]:
        if raw.get("floorPriceNanoTons"):
            assert by_name[raw["name"]].floor_ton == pytest.approx(
                raw["floorPriceNanoTons"] / 1e9
            )


async def test_previous_day_floor_gives_trend_without_own_history():
    """Площадка отдаёт вчерашний флор — тренд доступен с первого запуска."""
    adapter = FakeAdapter({"collections": FIXTURES["collections"]})
    previous = await adapter.previous_day_floors()

    expected = {
        raw["name"]
        for raw in FIXTURES["collections"]
        if raw.get("previousDayFloorPriceNanoTons")
    }
    assert set(previous) == expected


async def test_attribute_floors_survive_missing_endpoint():
    """Путь к моделям не подтверждён — его отсутствие не должно ломать разбор."""
    adapter = FakeAdapter({"backdrops": FIXTURES["backdrops"]})
    floors = await adapter.attribute_floors("Evil Eye")

    # symbols и models в подменённых ответах отсутствуют и кинут KeyError,
    # но фоны обязаны разобраться.
    assert all(item.kind is AttributeKind.BACKDROP for item in floors)


# --- Классификатор HAR на реальных путях ---------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/gifts/saling", "listings"),
        ("/api/v1/gifts/collections", "collections"),
        ("/api/v1/gifts/backdrops", "filter_floors"),
        ("/api/v1/gifts/symbols", "filter_floors"),
        ("/api/v1/balance", "balance"),
        # Реальные пути MRKT, которые раньше распознавались неверно:
        # счётчик уведомлений попадал в «ленту сделок», а страница
        # статистики конкурировала со списком лотов.
        ("/api/v1/feed", "activity"),
        ("/api/v1/activities/notifications-count", None),
        ("/api/v1/gift-statistics", None),
        ("/api/v1/team-events/active", None),
        ("/api/v1/in-app-notifications", None),
        ("/api/v1/stars-gifts", None),
        ("/api/v1/sticker-sets/collections", None),
    ],
)
def test_classifier_on_real_mrkt_paths(path: str, expected: str | None):
    assert classify(path) == expected


def test_default_endpoints_point_at_real_api():
    assert DEFAULT_ENDPOINTS.base_url == "https://api.tgmrkt.io"
    listings = DEFAULT_ENDPOINTS.get("listings")
    assert listings.method == "POST"
    assert listings.path == "/api/v1/gifts/saling"
    assert listings.json_path == "gifts"


def test_endpoints_are_overridable():
    """Правка путей должна работать без пересборки приложения."""
    patched = MarketEndpoints(
        base_url="https://api.example.io",
        endpoints={**DEFAULT_ENDPOINTS.endpoints},
    )
    assert patched.get("listings").path == "/api/v1/gifts/saling"


# --- Лента сделок --------------------------------------------------------


def test_activity_endpoint_is_confirmed_feed():
    """Путь ленты — /api/v1/feed, а не угаданный ранее /api/v1/activities.

    Без ленты у MRKT нет истории сделок, а значит нет ни скорости продаж,
    ни времени до продажи — 65% веса балла ликвидности.
    """
    spec = DEFAULT_ENDPOINTS.get("activity")
    assert spec.path == "/api/v1/feed"
    assert spec.method == "POST"
    assert spec.json_path == "items"


async def test_activity_request_body_matches_api_contract():
    adapter = FakeAdapter({"activity": FIXTURES["feed"]})
    await adapter.activity("Lush Bouquet", limit=20)

    _, _, body = adapter.calls[0]
    assert body["type"] == ["Sale", "Listing"]
    assert body["collectionNames"] == ["Lush Bouquet"]
    assert body["ordering"] == "Latest"
    for required in ("count", "cursor", "minPrice", "maxPrice"):
        assert required in body


async def test_activity_parses_sales_and_listings():
    adapter = FakeAdapter({"activity": FIXTURES["feed"]})
    events = await adapter.activity(limit=50)

    assert len(events) == len(FIXTURES["feed"]["items"])
    kinds = {event.kind for event in events}
    assert ActivityKind.SALE in kinds
    assert ActivityKind.LISTING in kinds


async def test_activity_price_is_buyer_facing_nanotons():
    """amount у продажи совпадает с salePrice подарка — цена с надбавкой."""
    adapter = FakeAdapter({"activity": FIXTURES["feed"]})
    events = await adapter.activity(limit=50)

    raw = FIXTURES["feed"]["items"][0]
    first = events[0]
    assert first.price_ton == pytest.approx(raw["amount"] / 1e9)
    assert first.price_ton == pytest.approx(raw["gift"]["salePrice"] / 1e9)


async def test_activity_keeps_gift_identity_for_antifraud():
    """Событию нужен идентификатор экземпляра, а не только модель.

    Отбраковка перепродаж своим же лотам сравнивает подарки по
    external_id: без него одинаковые модели разных подарков выглядят
    как повторный флип и вся ликвидная коллекция уходит в отсев.
    """
    adapter = FakeAdapter({"activity": FIXTURES["feed"]})
    events = await adapter.activity(limit=50)

    raw = FIXTURES["feed"]["items"][0]
    assert events[0].gift.external_id == raw["gift"]["id"]
    assert events[0].external_id == raw["id"]
    assert events[0].gift.number == raw["gift"]["number"]
    assert events[0].gift.collection == raw["gift"]["collectionName"]


async def test_unknown_event_type_is_skipped_not_counted_as_sale():
    """Выдуманная сделка завышает и скорость продаж, и историю цен."""
    payload = {
        "items": [
            {
                "type": "someNewThing",
                "id": "e-1",
                "amount": 1_000_000_000,
                "date": "2026-07-30T06:42:45Z",
                "gift": FIXTURES["feed"]["items"][0]["gift"],
            }
        ],
        "cursor": "",
    }
    adapter = FakeAdapter({"activity": payload})
    assert await adapter.activity(limit=50) == []


async def test_activity_follows_cursor_across_pages():
    first = {"items": FIXTURES["feed"]["items"][:4], "cursor": "page-2"}
    second = {"items": FIXTURES["feed"]["items"][4:], "cursor": ""}
    adapter = FakeAdapter({"activity": Pages([first, second])})

    events = await adapter.activity(limit=50)

    assert len(events) == len(FIXTURES["feed"]["items"])
    assert adapter.calls[1][2]["cursor"] == "page-2", "курсор должен уходить в запрос"


async def test_listings_follow_cursor_across_pages():
    """Курсор терялся при выборке массива — обход кончался на первой странице."""
    first = {"gifts": FIXTURES["saling"]["gifts"], "cursor": "page-2"}
    second = {"gifts": FIXTURES["saling"]["gifts"], "cursor": ""}
    adapter = FakeAdapter({"listings": Pages([first, second])})

    await adapter.listings("Evil Eye", limit=50)

    assert len(adapter.calls) == 2
    assert adapter.calls[1][2]["cursor"] == "page-2"


async def test_paging_stops_without_cursor():
    """Пустой курсор — конец выдачи, лишних запросов быть не должно."""
    adapter = FakeAdapter({"activity": {"items": FIXTURES["feed"]["items"], "cursor": ""}})
    await adapter.activity(limit=500)

    assert len(adapter.calls) == 1


# --- Торговля ------------------------------------------------------------


def test_trading_paths_match_the_mini_app():
    """Пути взяты из JS-бандла мини-аппа — это исполняемый код кнопок.

    Три из четырёх прежних предположений оказались неверны: /gifts/sell,
    /gifts/price и /gifts/unlist у площадки не существуют вовсе.
    """
    assert DEFAULT_ENDPOINTS.get("buy").path == "/api/v1/gifts/buy"
    assert DEFAULT_ENDPOINTS.get("sell").path == "/api/v1/gifts/sale"
    assert DEFAULT_ENDPOINTS.get("change_price").path == "/api/v1/gifts/sale/change-price"
    assert DEFAULT_ENDPOINTS.get("delist").path == "/api/v1/gifts/sale/cancel"
    assert DEFAULT_ENDPOINTS.get("inventory").path == "/api/v1/gifts"


async def test_buy_sends_ids_and_price_map():
    """Тело покупки: {ids: [...], prices: {id: нанотоны}} — как в бандле."""
    gift_id = "00000000-0000-4000-8000-000000000001"
    adapter = FakeAdapter({"buy": [{"userGift": {"id": gift_id}}]})
    listing = Listing(
        market=Market.MRKT,
        listing_id=gift_id,
        gift=Gift(collection="Evil Eye", external_id=gift_id),
        price_ton=12.5,
    )

    reference = await adapter.buy(listing)

    _, _, body = adapter.calls[0]
    assert body["ids"] == [gift_id]
    assert body["prices"] == {gift_id: 12_500_000_000}
    assert reference == gift_id


async def test_empty_buy_response_is_a_failure_not_a_purchase():
    """Лот перехватили: площадка отвечает 200 и пустым списком.

    Принять это за успех — значит завести позицию по подарку, которого у
    нас нет, и потом безуспешно пытаться его продать.
    """
    gift_id = "00000000-0000-4000-8000-000000000002"
    adapter = FakeAdapter({"buy": []})
    listing = Listing(
        market=Market.MRKT,
        listing_id=gift_id,
        gift=Gift(collection="Evil Eye", external_id=gift_id),
        price_ton=12.5,
    )

    with pytest.raises(MarketplaceError, match="уже продан"):
        await adapter.buy(listing)


async def test_listing_for_sale_sends_seller_price():
    """Площадка ждёт цену продавца, а приложение хранит покупательскую."""
    adapter = FakeAdapter({"sell": {"ids": ["G1"]}})
    await adapter.list_for_sale("G1", 10.2)

    _, _, body = adapter.calls[0]
    assert body["ids"] == ["G1"]
    assert body["price"] == pytest.approx(10_000_000_000, rel=1e-6)


async def test_rejected_listing_is_reported():
    adapter = FakeAdapter({"sell": {"ids": []}})
    with pytest.raises(MarketplaceError, match="не приняла"):
        await adapter.list_for_sale("G1", 10.2)


async def test_change_price_uses_new_price_field():
    adapter = FakeAdapter({"change_price": {"ids": ["G1"]}})
    await adapter.change_price("G1", 10.2)

    _, _, body = adapter.calls[0]
    assert body["ids"] == ["G1"]
    assert body["newPrice"] == pytest.approx(10_000_000_000, rel=1e-6)


async def test_delist_explains_the_two_minute_cooldown():
    """Отказ приходит пустым списком — пользователю нужна причина."""
    adapter = FakeAdapter({"delist": []})
    with pytest.raises(MarketplaceError, match="откат"):
        await adapter.delist("G1")


async def test_inventory_collects_listed_and_unlisted():
    """isListed — булево, поэтому нужны оба среза."""
    gift = FIXTURES["saling"]["gifts"][0]
    slice_response = {"gifts": [gift], "cursor": "", "total": 1}
    # Два запроса — два ответа: выставленное и лежащее без дела.
    adapter = FakeAdapter({"inventory": Pages([slice_response, slice_response])})
    owned = await adapter.inventory()

    flags = [body["isListed"] for _, _, body in adapter.calls]
    assert flags == [True, False]
    assert len(owned) == 2


async def test_top_offers_parse_bids():
    adapter = FakeAdapter(
        {"orders": [{"collectionName": "Evil Eye", "maxPrice": 9_000_000_000, "count": 3}]}
    )
    offers = await adapter.top_offers()

    assert len(offers) == 1
    assert offers[0].collection == "Evil Eye"
    assert offers[0].price_ton == pytest.approx(9.0)
    assert offers[0].amount == 3


async def test_unrecognised_offer_response_yields_nothing():
    """Схема ответа не подтверждена — непонятный ответ не должен ломать скан."""
    adapter = FakeAdapter({"orders": [{"unexpected": "shape"}]})
    assert await adapter.top_offers() == []
