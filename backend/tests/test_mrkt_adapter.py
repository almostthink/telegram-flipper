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
from app.adapters.base import MarketEndpoints
from app.adapters.har import classify
from app.adapters.mrkt import (
    BUYER_MARKUP,
    DEFAULT_ENDPOINTS,
    FEE_AS_SELL_SIDE,
    MrktAdapter,
    parse_mrkt_gift,
)
from app.analytics import pricing
from app.domain import AttributeKind, Market

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mrkt_responses.json").read_text(encoding="utf-8")
)


@pytest.fixture
def adapter() -> MrktAdapter:
    return MrktAdapter(endpoints=DEFAULT_ENDPOINTS, fee_sell=FEE_AS_SELL_SIDE)


class FakeAdapter(MrktAdapter):
    """Адаптер с подменённым транспортом: сеть в тестах не нужна."""

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__(endpoints=DEFAULT_ENDPOINTS, fee_sell=FEE_AS_SELL_SIDE)
        self.responses = responses
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    async def request(self, endpoint, *, params=None, json_body=None):
        self.calls.append((endpoint, params, json_body))
        if endpoint not in self.responses:
            raise KeyError(endpoint)
        payload = self.responses[endpoint]
        spec = self.endpoints.get(endpoint)
        from app.adapters.base import dig

        return dig(payload, spec.json_path)


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
