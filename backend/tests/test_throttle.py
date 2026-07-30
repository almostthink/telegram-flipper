"""Проверки бережного обращения с площадкой.

На реальном запуске проход упёрся в лимит частоты: 25 коллекций × (лоты
+ лента + три атрибута) дали больше трёхсот запросов подряд, площадка
начала отвечать 429, и лента сделок не дошла вовсе — ноль продаж за цикл
при полутора тысячах разобранных лотов.

Лечится с двух сторон: меньше запросов и умение притормаживать.
"""

from __future__ import annotations

import pytest
from app.adapters import registry
from app.adapters.base import MAX_INTERVAL_SEC, RELAX_AFTER_OK, RateLimiter
from app.domain import Market
from app.ingest import scanner as scanner_mod

# --- Адаптация частоты ---------------------------------------------------


def test_penalty_slows_future_requests():
    """Отказ по частоте должен менять поведение, а не только текущий запрос."""
    limiter = RateLimiter(1.0)
    limiter.penalize()

    assert limiter.min_interval > 1.0


def test_slowdown_has_a_ceiling():
    limiter = RateLimiter(1.0)
    for _ in range(50):
        limiter.penalize()

    assert limiter.min_interval <= MAX_INTERVAL_SEC


def test_recovery_needs_a_streak_of_successes():
    """Ускоряться после первого же удачного ответа — снова упереться в лимит."""
    limiter = RateLimiter(1.0)
    limiter.penalize()
    slowed = limiter.min_interval

    limiter.relax()
    assert limiter.min_interval == slowed, "одного успеха мало"

    for _ in range(RELAX_AFTER_OK):
        limiter.relax()
    assert limiter.min_interval < slowed


def test_recovery_stops_at_the_base_interval():
    limiter = RateLimiter(1.0)
    for _ in range(200):
        limiter.relax()

    assert limiter.min_interval == 1.0


def test_limiter_outlives_the_adapter():
    """Адаптеры создаются заново каждый проход.

    Со счётчиком внутри адаптера выученное замедление забывалось бы
    сразу, и следующий проход снова упирался в тот же лимит.
    """
    registry._LIMITERS.clear()
    first = registry.limiter_for(Market.MRKT, 1.0)
    first.penalize()
    slowed = first.min_interval

    second = registry.limiter_for(Market.MRKT, 1.0)
    assert second is first
    assert second.min_interval == slowed


def test_changed_setting_resets_the_limiter():
    registry._LIMITERS.clear()
    registry.limiter_for(Market.MRKT, 1.0).penalize()
    fresh = registry.limiter_for(Market.MRKT, 2.0)

    assert fresh.base_interval == 2.0
    assert fresh.min_interval == 2.0


# --- Объём запросов ------------------------------------------------------


def test_listing_depth_is_two_pages():
    """Страница отдаёт 20 лотов; глубже флор-зоны смотреть незачем."""
    assert scanner_mod.LISTINGS_PER_COLLECTION == 40


def test_attribute_floors_are_spread_across_cycles():
    assert scanner_mod.ATTRIBUTE_SCAN_EVERY > 1


async def test_activity_is_fetched_once_per_scan(monkeypatch):
    """Лента общая на весь рынок — по коллекции её спрашивать не надо.

    Именно эти два десятка одинаковых запросов и упирали проход в лимит.
    """
    from app.config import MarketplaceConfig, Settings
    from app.storage.db import init_db

    await init_db()

    settings = Settings(paper_mode=True)
    settings.marketplaces = {"mrkt": MarketplaceConfig(enabled=True, trade_enabled=True)}
    scanner = scanner_mod.Scanner(settings)

    activity_calls: list[str | None] = []

    class FakeAdapter:
        name = Market.MRKT

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def activity(self, collection=None, *, limit=100):
            activity_calls.append(collection)
            return []

        async def listings(self, collection, *, limit=100):
            return []

        async def attribute_floors(self, collection):
            return []

    monkeypatch.setattr(
        registry, "build_enabled", lambda _settings: {Market.MRKT: FakeAdapter()}
    )

    await scanner.scan_collections(["A", "B", "C", "D", "E"])

    assert len(activity_calls) == 1, "лента должна забираться одним обходом"
    assert activity_calls[0] is None, "по всему рынку, а не по коллекции"


# --- Запись флоров атрибутов ---------------------------------------------


@pytest.fixture
async def db(tmp_path, monkeypatch):
    from app import paths
    from app.storage.db import dispose_db, init_db

    await dispose_db()
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    await init_db()
    yield
    await dispose_db()


def attribute(name: str, floor: float):
    from app.domain import AttributeFloor, AttributeKind

    return AttributeFloor(
        market=Market.MRKT,
        collection="Evil Eye",
        kind=AttributeKind.BACKDROP,
        name=name,
        floor_ton=floor,
    )


async def test_unchanged_floors_are_not_rewritten(db):
    """Восемь сотен строк на коллекцию, из которых меняются единицы."""
    from app.storage import repo
    from app.storage.db import session_scope

    batch = [attribute(f"B{i}", 10.0) for i in range(5)]

    async with session_scope() as session:
        assert await repo.record_attribute_floors(session, batch) == 5

    async with session_scope() as session:
        assert await repo.record_attribute_floors(session, batch) == 0


async def test_changed_floor_is_recorded(db):
    from app.storage import repo
    from app.storage.db import session_scope

    async with session_scope() as session:
        await repo.record_attribute_floors(session, [attribute("B1", 10.0)])

    async with session_scope() as session:
        assert await repo.record_attribute_floors(session, [attribute("B1", 12.0)]) == 1


async def test_rounding_noise_is_not_a_change(db):
    from app.storage import repo
    from app.storage.db import session_scope

    async with session_scope() as session:
        await repo.record_attribute_floors(session, [attribute("B1", 10.0)])

    async with session_scope() as session:
        assert await repo.record_attribute_floors(session, [attribute("B1", 10.001)]) == 0


async def test_stable_floor_stays_readable(db):
    """Читать надо последнее известное, а не последнее записанное недавно.

    Иначе атрибут со стабильной ценой пропадает из оценки, и множитель
    редкости теряется на ровном месте.
    """
    from datetime import timedelta

    from app.domain import utcnow
    from app.storage import repo
    from app.storage.db import session_scope
    from app.storage.models import AttributeFloorSnapshot

    async with session_scope() as session:
        session.add(
            AttributeFloorSnapshot(
                market="mrkt",
                collection="Evil Eye",
                kind="backdrop",
                name="Onyx Black",
                floor_ton=42.0,
                captured_at=utcnow() - timedelta(days=3),
            )
        )

    async with session_scope() as session:
        floors = await repo.attribute_floor_map(session, "Evil Eye")

    assert floors[("backdrop", "Onyx Black")] == pytest.approx(42.0)
