"""Проверки адаптера Portals на реальных ответах площадки.

Фикстура снята с записи трафика portal-market.com. Обезличены только
идентификаторы лотов и TON-адреса; структура и имена полей подлинные.

Прежняя версия адаптера обращалась к несуществующему домену
``portals-market.com`` и падала с ошибкой разрешения имени. Настоящий
адрес — ``portal-market.com``, и первый же тест это фиксирует.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.base import dig
from app.adapters.portals import DEFAULT_ENDPOINTS, PortalsAdapter, parse_portals_gift
from app.domain import AttributeKind, Market

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "portals_responses.json").read_text(encoding="utf-8")
)


class FakePortals(PortalsAdapter):
    """Адаптер с подменённым транспортом: сеть в тестах не нужна."""

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__(endpoints=DEFAULT_ENDPOINTS, fee_sell=0.05)
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def request(self, endpoint, *, params=None, json_body=None, raw=False):
        self.calls.append((endpoint, params))
        if endpoint not in self.responses:
            raise KeyError(endpoint)
        payload = self.responses[endpoint]
        return payload if raw else dig(payload, self.endpoints.get(endpoint).json_path)


def test_base_url_is_the_real_domain():
    """Домен пишется portal-market, а не portals-market."""
    assert DEFAULT_ENDPOINTS.base_url == "https://portal-market.com"


def test_portals_is_read_only():
    """Запросы на покупку записью не подтверждены — торговать нельзя."""
    assert PortalsAdapter.supports_trading is False


# --- Разбор ------------------------------------------------------------------


def test_parses_attributes_from_list_form():
    raw = FIXTURES["search"]["results"][0]
    gift = parse_portals_gift(raw)

    kinds = {
        attribute.kind
        for attribute in (gift.model, gift.backdrop, gift.symbol)
        if attribute is not None
    }
    assert AttributeKind.MODEL in kinds

    # Номер выпуска лежит в external_collection_number.
    assert gift.number == raw["external_collection_number"]


def test_rarity_is_read_from_attributes():
    raw = FIXTURES["search"]["results"][0]
    gift = parse_portals_gift(raw)

    source = {item["type"]: item for item in raw["attributes"]}
    if "model" in source and source["model"].get("rarity_per_mille") is not None:
        assert gift.model.rarity_permille == source["model"]["rarity_per_mille"]


async def test_collection_floors_parse_string_prices():
    """Цены здесь строки в TON, а не нанотоны — переводить нечего."""
    adapter = FakePortals({"collections": FIXTURES["collections"]})
    floors = await adapter.collection_floors()

    assert floors
    by_name = {item.collection: item for item in floors}
    for raw in FIXTURES["collections"]["collections"]:
        if raw.get("floor_price"):
            assert by_name[raw["name"]].floor_ton == pytest.approx(float(raw["floor_price"]))


async def test_collection_ids_are_remembered_for_search():
    """Поиск лотов принимает идентификатор, а не имя коллекции."""
    adapter = FakePortals({"collections": FIXTURES["collections"]})
    await adapter.collection_floors()

    first = FIXTURES["collections"]["collections"][0]
    assert await adapter.collection_id(first["name"]) == first["id"]


async def test_listings_use_collection_id_in_query():
    adapter = FakePortals(
        {"collections": FIXTURES["collections"], "search": FIXTURES["search"]}
    )
    name = FIXTURES["collections"]["collections"][0]["name"]
    await adapter.listings(name, limit=50)

    search_call = next(params for endpoint, params in adapter.calls if endpoint == "search")
    assert search_call["collection_ids"] == FIXTURES["collections"]["collections"][0]["id"]
    assert search_call["sort_by"] == "price asc", "нужны сначала дешёвые лоты"
    assert search_call["status"] == "listed"


async def test_listings_skip_own_lots():
    adapter = FakePortals(
        {"collections": FIXTURES["collections"], "search": FIXTURES["search"]}
    )
    name = FIXTURES["collections"]["collections"][0]["name"]
    listings = await adapter.listings(name, limit=50)

    ids = {item.listing_id for item in listings}
    assert "00000000-0000-4000-8000-000000000091" not in ids


async def test_resale_lock_is_read_from_unlocks_at():
    """unlocks_at — момент, до которого лот перепродать нельзя."""
    adapter = FakePortals(
        {"collections": FIXTURES["collections"], "search": FIXTURES["search"]}
    )
    name = FIXTURES["collections"]["collections"][0]["name"]
    listings = await adapter.listings(name, limit=50)

    locked = next(
        item for item in listings if item.listing_id == "00000000-0000-4000-8000-000000000090"
    )
    assert locked.resale_available_at is not None
    assert not locked.resalable_at(datetime(2026, 8, 1, tzinfo=UTC))


async def test_listings_carry_market_and_price():
    adapter = FakePortals(
        {"collections": FIXTURES["collections"], "search": FIXTURES["search"]}
    )
    name = FIXTURES["collections"]["collections"][0]["name"]
    listings = await adapter.listings(name, limit=50)

    assert listings
    first = listings[0]
    assert first.market is Market.PORTALS
    assert first.price_ton == pytest.approx(float(FIXTURES["search"]["results"][0]["price"]))


async def test_missing_collection_returns_nothing_not_error():
    """Неизвестная коллекция не должна ронять сканирование."""
    adapter = FakePortals({"collections": FIXTURES["collections"]})
    assert await adapter.listings("Такой коллекции нет") == []


async def test_symbol_floors_parse():
    adapter = FakePortals({"symbols": FIXTURES["symbols"]})
    floors = await adapter.attribute_floors("Magic Potion")

    assert floors
    assert all(item.kind is AttributeKind.SYMBOL for item in floors)
    assert all(item.floor_ton > 0 for item in floors)


def test_host_hint_matches_the_real_domain():
    """Подстрока фильтра обязана входить в домен площадки.

    «portals» в «portal-market.com» не входит: из-за этой опечатки фильтр
    HAR не срабатывал, включался фолбэк без фильтра, и в конфиг попадали
    адреса посторонних сервисов.
    """
    from app.api.routes_auth import _host_hint
    from app.domain import Market

    for market, base in (
        (Market.PORTALS, "https://portal-market.com"),
        (Market.MRKT, "https://api.tgmrkt.io"),
    ):
        hint = _host_hint(market)
        assert hint and hint in base, f"фильтр {hint!r} не входит в {base}"


def test_activity_endpoint_is_configured():
    """Без ленты сделок недоступны 65% балла ликвидности."""
    activity = DEFAULT_ENDPOINTS.get("activity")
    assert activity.path == "/api/market/actions/"


async def test_activity_requests_only_trades():
    adapter = FakePortals(
        {"collections": FIXTURES["collections"], "activity": {"actions": []}}
    )
    await adapter.activity(limit=50)

    _, params = next(call for call in adapter.calls if call[0] == "activity")
    assert params["action_types"] == "buy,sell"


async def test_activity_parses_sales():
    adapter = FakePortals(
        {
            "collections": FIXTURES["collections"],
            "activity": {
                "actions": [
                    {
                        "id": "act-1",
                        "type": "buy",
                        "amount": "49.98",
                        "created_at": "2026-07-29T16:20:23Z",
                        "nft": FIXTURES["search"]["results"][0],
                    }
                ]
            },
        }
    )
    events = await adapter.activity(limit=50)

    assert len(events) == 1
    assert events[0].price_ton == pytest.approx(49.98)
    assert events[0].external_id == "act-1"
