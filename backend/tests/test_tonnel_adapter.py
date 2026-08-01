"""Проверки адаптера Tonnel на реальных ответах площадки.

Фикстура снята с записи трафика gifts2.tonnel.network. Обезличены только
идентификаторы лотов; структура и имена полей подлинные. Учётных данных в
ней нет — у Tonnel они уходят в теле запроса, и попасть в ответ не могут.

Площадка отличается от остальных тремя вещами, и каждая из них здесь
проверяется: авторизация в теле, фильтры в стиле MongoDB и редкость,
зашитая прямо в имя атрибута процентами.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.adapters.base import AuthPlacement
from app.adapters.tonnel import (
    DEFAULT_ENDPOINTS,
    TonnelAdapter,
    min_next_bid,
    parse_attribute,
    parse_tonnel_gift,
)
from app.domain import AttributeKind, Market

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "tonnel_responses.json").read_text(encoding="utf-8")
)


class FakeTonnel(TonnelAdapter):
    """Адаптер с подменённым транспортом: сеть в тестах не нужна."""

    def __init__(self, responses: dict[str, object], *, token: str = "initdata") -> None:
        super().__init__(endpoints=DEFAULT_ENDPOINTS, fee_sell=0.06, auth_header=token)
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def request(self, endpoint, *, params=None, json_body=None, raw=False, path_params=None):
        body = self._inject_body_auth(endpoint, json_body)
        self.calls.append((endpoint, body))
        if endpoint not in self.responses:
            raise KeyError(endpoint)
        from app.adapters.base import dig

        payload = self.responses[endpoint]
        return payload if raw else dig(payload, self.endpoints.get(endpoint).json_path)


def body_of(adapter: FakeTonnel, endpoint: str) -> dict:
    return next(body for name, body in adapter.calls if name == endpoint)


# --- Авторизация ----------------------------------------------------------


def test_auth_goes_into_the_body():
    """Заголовка Authorization у Tonnel нет вовсе — отсюда и путаница."""
    assert TonnelAdapter.auth_placement is AuthPlacement.BODY


async def test_listings_carry_user_auth_field():
    """Выдача лотов ждёт initData под именем user_auth."""
    adapter = FakeTonnel({"listings": FIXTURES["gifts"]})
    await adapter.listings("Witch Hat", limit=10)

    body = body_of(adapter, "listings")
    assert body["user_auth"] == "initdata"
    assert "authData" not in body


async def test_other_endpoints_carry_auth_data_field():
    """Остальные — под именем authData. Оба варианта из одного сеанса."""
    adapter = FakeTonnel({"balance": FIXTURES["balance"]})
    await adapter.balance()

    assert body_of(adapter, "balance")["authData"] == "initdata"


def test_auth_header_is_not_sent():
    """Отправлять его незачем: площадка его не читает."""
    adapter = FakeTonnel({}, token="initdata")
    assert "Authorization" not in adapter._default_headers()


# --- Редкость в имени -----------------------------------------------------


def test_rarity_percent_becomes_permille():
    """Проценты у Tonnel, промилле у остальных — путать нельзя.

    Ошибка здесь ровно в десять раз, и она прямо искажает оценку.
    """
    attribute = parse_attribute("Pyromancy (1.5%)", AttributeKind.MODEL)

    assert attribute is not None
    assert attribute.name == "Pyromancy"
    assert attribute.rarity_permille == pytest.approx(15.0)


def test_attribute_without_rarity_still_parses():
    attribute = parse_attribute("Just A Name", AttributeKind.BACKDROP)
    assert attribute is not None
    assert attribute.name == "Just A Name"
    assert attribute.rarity_permille is None


def test_missing_attribute_is_none():
    assert parse_attribute(None, AttributeKind.MODEL) is None
    assert parse_attribute("", AttributeKind.MODEL) is None


def test_real_gift_parses():
    raw = FIXTURES["gifts"][0]
    gift = parse_tonnel_gift(raw)

    assert gift.collection == raw["name"]
    assert gift.number == raw["gift_num"]
    assert gift.model is not None
    assert "(" not in gift.model.name, "редкость должна быть убрана из имени"


# --- Флоры ----------------------------------------------------------------


async def test_collection_floor_is_the_minimum_across_models():
    """Площадка отдаёт флор по паре «коллекция_модель», а не по коллекции."""
    adapter = FakeTonnel({"collections": FIXTURES["filterStats"]})
    floors = await adapter.collection_floors()

    assert floors
    by_name = {item.collection: item for item in floors}

    data = FIXTURES["filterStats"]["data"]
    expected: dict[str, float] = {}
    for key, stats in data.items():
        name = key.split("_", 1)[0].strip()
        price = float(stats["floorPrice"])
        expected[name] = min(expected.get(name, price), price)

    for name, price in expected.items():
        assert by_name[name].floor_ton == pytest.approx(price)


async def test_floors_carry_the_market():
    adapter = FakeTonnel({"collections": FIXTURES["filterStats"]})
    floors = await adapter.collection_floors()
    assert all(item.market is Market.TONNEL for item in floors)


# --- Лоты -----------------------------------------------------------------


async def test_listings_use_a_mongo_style_filter():
    """Фильтр уходит строкой с JSON — это запрос MongoDB, а не набор полей."""
    adapter = FakeTonnel({"listings": FIXTURES["gifts"]})
    await adapter.listings("Witch Hat", limit=10)

    body = body_of(adapter, "listings")
    query = json.loads(body["filter"])
    assert query["price"] == {"$exists": True}
    assert query["buyer"] == {"$exists": False}
    assert query["gift_name"] == "Witch Hat"


async def test_listings_parse_price_and_lock():
    adapter = FakeTonnel({"listings": FIXTURES["gifts"]})
    listings = await adapter.listings("Witch Hat", limit=10)

    assert listings
    raw = FIXTURES["gifts"][0]
    first = listings[0]
    assert first.price_ton == pytest.approx(raw["price"])
    assert first.market is Market.TONNEL
    # can_transfer_since — это и есть блокировка перепродажи.
    assert first.resale_available_at is not None


# --- Аукцион --------------------------------------------------------------


def test_first_bid_is_the_starting_price():
    assert min_next_bid(highest_ton=0.0, starting_ton=2.5, bids=0) == pytest.approx(2.5)


def test_next_bid_is_five_percent_above():
    """Шаг ставки процентный — цена растёт скачками, а не на копейку."""
    assert min_next_bid(highest_ton=10.0, starting_ton=1.0, bids=3) == pytest.approx(10.5)


def test_bid_is_rounded_up_not_arithmetically():
    """Арифметическое округление дало бы значение ниже требуемого.

    Площадка такую ставку отвергнет, и бот будет молча промахиваться.
    """
    value = min_next_bid(highest_ton=3.3333, starting_ton=1.0, bids=1)

    assert value >= 3.3333 * 1.05
    assert value == pytest.approx(3.5)


async def test_auction_filter_selects_active_auctions():
    adapter = FakeTonnel({"auctions": [], "listings": []})
    await adapter.auctions(limit=10)

    query = json.loads(body_of(adapter, "listings")["filter"])
    assert query["auction_id"] == {"$exists": True}
    assert query["status"] == "active"


async def test_auction_parses_bid_state():
    lot = dict(FIXTURES["gifts"][0])
    lot["auction"] = {
        "auction_id": "A1",
        "startingBid": 2.0,
        "auctionEndTime": "2026-08-01T10:00:00.000Z",
        "bidHistory": [
            {"amount": 2.0, "bidder": 1, "timestamp": "2026-07-30T10:00:00.000Z"},
            {"amount": 4.0, "bidder": 2, "timestamp": "2026-07-30T11:00:00.000Z"},
        ],
    }
    adapter = FakeTonnel({"listings": [lot]})

    auctions = await adapter.auctions(limit=10)

    assert len(auctions) == 1
    lot_state = auctions[0]
    assert lot_state.auction_id == "A1"
    assert lot_state.highest_bid_ton == pytest.approx(4.0)
    assert lot_state.min_bid_ton == pytest.approx(4.2)
    assert lot_state.bids == 2
    assert lot_state.ends_at is not None


# --- Сделки и баланс ------------------------------------------------------


async def test_sales_parse_from_history():
    adapter = FakeTonnel({"activity": FIXTURES["saleHistory"]})
    events = await adapter.activity(limit=50)

    assert events
    raw = FIXTURES["saleHistory"][0]
    assert events[0].price_ton == pytest.approx(raw["price"])
    assert events[0].gift.collection == raw["gift_name"]


async def test_balance_is_read():
    adapter = FakeTonnel({"balance": FIXTURES["balance"]})
    balance = await adapter.balance()
    assert balance.ton == pytest.approx(FIXTURES["balance"]["balance"])


def test_endpoints_point_at_the_real_api():
    assert DEFAULT_ENDPOINTS.base_url == "https://gifts2.tonnel.network"
    assert DEFAULT_ENDPOINTS.get("listings").path == "/api/pageGifts"
    assert DEFAULT_ENDPOINTS.get("collections").path == "/api/filterStats"
    assert DEFAULT_ENDPOINTS.get("auction_bid").path == "/api/auction/bid"


# --- Торговля -------------------------------------------------------------
#
# Ставка подтверждена записью трафика целиком: тело, домен и ответ.
# Остальные тела восстановлены из кода мини-аппа — там они собраны явно.


def test_trading_lives_on_another_host():
    """Чтение и торговля у Tonnel — разные серверы.

    Запрос на покупку по адресу читающего сервера просто не доходит, а
    выглядит это как молчаливый отказ площадки.
    """
    assert DEFAULT_ENDPOINTS.base_url == "https://gifts2.tonnel.network"
    assert DEFAULT_ENDPOINTS.get("auction_bid").base_url == "https://rs-api.tonnel.network"
    assert DEFAULT_ENDPOINTS.get("buy").base_url == "https://rs-api.tonnel.network"
    # Чтение остаётся на общем домене.
    assert DEFAULT_ENDPOINTS.get("listings").base_url == ""


async def test_bid_body_matches_the_recorded_one():
    """Ровно то, что ушло с сайта: id аукциона, сумма строкой и актив."""
    adapter = FakeTonnel({"auction_bid": {"status": "success", "message": "Success"}})
    await adapter.place_bid("4PWZJQAY", 3.045)

    body = body_of(adapter, "auction_bid")
    assert body == {
        "authData": "initdata",
        "auction_id": "4PWZJQAY",
        "amount": "3.045",
        "asset": "TON",
    }


def test_amount_never_leaks_binary_tail():
    """3.045 как float печатается 3.0449999999999999 — и ставка ниже минимума."""
    from app.adapters.tonnel import _as_amount

    assert _as_amount(2.9 * 1.05) == "3.045"
    assert _as_amount(10.0) == "10"


async def test_rejection_is_not_mistaken_for_success():
    """Площадка отвечает 200 и на отказ — приговор лежит в теле.

    Без проверки неудачная покупка выглядела бы удачной, и приложение
    завело бы позицию на подарок, которого у него нет.
    """
    adapter = FakeTonnel(
        {"auction_bid": {"status": "error", "message": "Bid too low"}}
    )
    with pytest.raises(Exception, match="Bid too low"):
        await adapter.place_bid("A1", 1.0)


async def test_purchase_carries_the_signature():
    """Поле wtf — зашифрованная отметка времени. Без неё покупка не проходит."""
    from app.adapters.cryptojs import decrypt
    from app.adapters.tonnel import SIGNING_KEY
    from app.domain import Listing

    adapter = FakeTonnel({"buy": {"status": "success"}})
    listing = Listing(
        market=Market.TONNEL,
        listing_id="10358715",
        gift=parse_tonnel_gift(FIXTURES["gifts"][0]),
        price_ton=3.5,
    )
    await adapter.buy(listing)

    body = body_of(adapter, "buy")
    assert body["price"] == 3.5
    assert body["asset"] == "TON"
    assert decrypt(body["wtf"], SIGNING_KEY) == str(body["timestamp"])


async def test_listing_for_sale_is_signed_too():
    from app.adapters.cryptojs import decrypt
    from app.adapters.tonnel import SIGNING_KEY

    adapter = FakeTonnel({"sell": {"status": "success"}})
    await adapter.list_for_sale("10358715", 4.2)

    body = body_of(adapter, "sell")
    assert body["gift_id"] == 10358715, "идентификатор лота у Tonnel числовой"
    assert body["price"] == 4.2
    assert decrypt(body["wtf"], SIGNING_KEY) == str(body["timestamp"])


async def test_no_collection_order_book():
    """Заявка у Tonnel адресная — «куплю любой из коллекции» здесь не бывает."""
    from app.adapters.tonnel import TonnelAdapter

    adapter = FakeTonnel({})
    assert await adapter.top_offers() == []
    assert TonnelAdapter.supports_collection_orders is False
