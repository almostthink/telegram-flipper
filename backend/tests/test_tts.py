"""Проверки восстановления времени до продажи.

TTS — 30% балла ликвидности и единственная величина, которая отвечает на
вопрос «через сколько я выйду из позиции». Прямого поля у площадок нет,
поэтому оно собирается из пары событий: выставили → продали.

До появления ленты MRKT пару искали по совпадению цены. В ликвидной
коллекции цены повторяются, и продажа могла сопоставиться с чужим лотом:
величина получалась не измеренной, а угаданной.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.domain import ActivityEvent, ActivityKind, Gift, Market, utcnow
from app.storage import repo
from app.storage.db import dispose_db, init_db, session_scope
from app.storage.models import ListingSnapshot, SaleRecord
from sqlalchemy import select


@pytest.fixture
async def db(tmp_path, monkeypatch):
    from app import paths

    await dispose_db()
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    await init_db()
    yield
    await dispose_db()


def event(
    kind: ActivityKind,
    *,
    gift_id: str,
    hours_ago: float,
    price_ton: float = 10.0,
    collection: str = "Lush Bouquet",
    event_id: str | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        market=Market.MRKT,
        kind=kind,
        gift=Gift(collection=collection, external_id=gift_id),
        price_ton=price_ton,
        happened_at=utcnow() - timedelta(hours=hours_ago),
        external_id=event_id or f"{kind.value}-{gift_id}-{hours_ago}",
    )


async def tts_of(gift_id: str) -> float | None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(SaleRecord).where(SaleRecord.gift_external_id == gift_id)
            )
        ).scalars()
        return next(iter(rows)).tts_hours


# --- Измерение по событиям ------------------------------------------------


async def test_tts_measured_from_listing_and_sale_of_same_gift(db):
    events = [
        event(ActivityKind.SALE, gift_id="G1", hours_ago=2),
        event(ActivityKind.LISTING, gift_id="G1", hours_ago=8),
    ]
    async with session_scope() as session:
        await repo.record_sales(session, events)
        await repo.record_listing_events(session, events)

    async with session_scope() as session:
        updated = await repo.attach_tts(session, "mrkt", "Lush Bouquet")

    assert updated == 1
    assert await tts_of("G1") == pytest.approx(6.0, abs=0.01)


async def test_sale_of_another_gift_at_the_same_price_is_not_matched(db):
    """Совпадение цены — не основание считать это тем же лотом.

    Ровно эта ошибка и делала TTS выдуманным: в ликвидной коллекции
    десяток лотов стоит одинаково.
    """
    events = [
        event(ActivityKind.LISTING, gift_id="G1", hours_ago=50, price_ton=10.0),
        event(ActivityKind.SALE, gift_id="G2", hours_ago=1, price_ton=10.0),
    ]
    async with session_scope() as session:
        await repo.record_sales(session, events)
        await repo.record_listing_events(session, events)

    async with session_scope() as session:
        await repo.attach_tts(session, "mrkt", "Lush Bouquet")

    assert await tts_of("G2") is None


async def test_listing_after_sale_is_not_negative_time(db):
    """Подарок перевыставили уже после продажи — пары нет."""
    events = [
        event(ActivityKind.LISTING, gift_id="G1", hours_ago=1),
        event(ActivityKind.SALE, gift_id="G1", hours_ago=5),
    ]
    async with session_scope() as session:
        await repo.record_sales(session, events)
        await repo.record_listing_events(session, events)

    async with session_scope() as session:
        assert await repo.attach_tts(session, "mrkt", "Lush Bouquet") == 0

    assert await tts_of("G1") is None


async def test_relisting_moves_the_clock_forward(db):
    """Лот сняли и выставили заново — отсчёт начинается со второго раза."""
    async with session_scope() as session:
        await repo.record_listing_events(
            session, [event(ActivityKind.LISTING, gift_id="G1", hours_ago=100)]
        )
    async with session_scope() as session:
        await repo.record_listing_events(
            session, [event(ActivityKind.LISTING, gift_id="G1", hours_ago=10)]
        )

    async with session_scope() as session:
        await repo.record_sales(session, [event(ActivityKind.SALE, gift_id="G1", hours_ago=4)])
    async with session_scope() as session:
        await repo.attach_tts(session, "mrkt", "Lush Bouquet")

    assert await tts_of("G1") == pytest.approx(6.0, abs=0.01)


async def test_older_listing_event_does_not_overwrite_newer(db):
    """Лента приходит от свежих к старым — старое событие затирать нельзя."""
    async with session_scope() as session:
        await repo.record_listing_events(
            session, [event(ActivityKind.LISTING, gift_id="G1", hours_ago=10)]
        )
    async with session_scope() as session:
        await repo.record_listing_events(
            session, [event(ActivityKind.LISTING, gift_id="G1", hours_ago=100)]
        )

    async with session_scope() as session:
        await repo.record_sales(session, [event(ActivityKind.SALE, gift_id="G1", hours_ago=4)])
    async with session_scope() as session:
        await repo.attach_tts(session, "mrkt", "Lush Bouquet")

    assert await tts_of("G1") == pytest.approx(6.0, abs=0.01)


async def test_absurdly_long_hold_is_rejected(db):
    """Больше месяца — почти наверняка ошибка сопоставления, а не сделка."""
    events = [
        event(ActivityKind.LISTING, gift_id="G1", hours_ago=24 * 40),
        event(ActivityKind.SALE, gift_id="G1", hours_ago=1),
    ]
    async with session_scope() as session:
        await repo.record_sales(session, events)
        await repo.record_listing_events(session, events)

    async with session_scope() as session:
        assert await repo.attach_tts(session, "mrkt", "Lush Bouquet") == 0


# --- Запасной способ ------------------------------------------------------


async def test_price_matching_still_works_without_listing_events(db):
    """У Portals лента короче: без запаса часть сделок осталась бы без TTS."""
    sale = event(ActivityKind.SALE, gift_id="G9", hours_ago=1, price_ton=12.5)
    async with session_scope() as session:
        await repo.record_sales(session, [sale])
        session.add(
            ListingSnapshot(
                market="mrkt",
                listing_id="G9",
                collection="Lush Bouquet",
                price_ton=12.5,
                listed_at=utcnow() - timedelta(hours=7),
                seen_at=utcnow(),
            )
        )

    async with session_scope() as session:
        assert await repo.attach_tts(session, "mrkt", "Lush Bouquet") == 1

    assert await tts_of("G9") == pytest.approx(6.0, abs=0.01)


# --- Запись событий -------------------------------------------------------


async def test_only_listing_events_are_stored(db):
    """Продажи в таблицу листингов попадать не должны."""
    events = [
        event(ActivityKind.SALE, gift_id="G1", hours_ago=1),
        event(ActivityKind.LISTING, gift_id="G2", hours_ago=2),
    ]
    async with session_scope() as session:
        assert await repo.record_listing_events(session, events) == 1


async def test_events_without_gift_id_are_skipped(db):
    """Без идентификатора экземпляра запись бесполезна и мешает дедупликации."""
    anonymous = ActivityEvent(
        market=Market.MRKT,
        kind=ActivityKind.LISTING,
        gift=Gift(collection="Lush Bouquet", external_id=""),
        price_ton=10.0,
        happened_at=utcnow(),
    )
    async with session_scope() as session:
        assert await repo.record_listing_events(session, [anonymous]) == 0


# --- Опора спроса ---------------------------------------------------------


async def test_best_offer_reaches_liquidity(db):
    """Заявка на покупку должна доходить до балла, а не теряться по пути.

    Компонент bid_support весит 20%, и до подключения книги заявок он
    всегда отсутствовал — балл считался по трём компонентам из четырёх.
    """
    from app.analytics import liquidity as liquidity_mod
    from app.domain import CollectionFloor, Market

    async with session_scope() as session:
        await repo.record_floors(
            session,
            [CollectionFloor(market=Market.MRKT, collection="Lush Bouquet", floor_ton=10.0)],
            offers={"Lush Bouquet": 9.0},
        )

    async with session_scope() as session:
        assert await repo.best_offer(session, "Lush Bouquet") == pytest.approx(9.0)

    measured = liquidity_mod.compute(
        "Lush Bouquet", sales=[], listings=[], floor_ton=10.0, best_offer_ton=9.0
    )
    assert "bid_support" not in measured.missing
    assert measured.parts["bid_support"] > 0


async def test_missing_offer_stays_unknown_not_zero(db):
    """Нет данных — компонент исключается, а не обнуляет коллекцию."""
    from app.analytics import liquidity as liquidity_mod
    from app.domain import CollectionFloor, Market

    async with session_scope() as session:
        await repo.record_floors(
            session,
            [CollectionFloor(market=Market.MRKT, collection="Ice Cream", floor_ton=10.0)],
        )

    async with session_scope() as session:
        assert await repo.best_offer(session, "Ice Cream") is None

    metrics = liquidity_mod.compute("Ice Cream", sales=[], listings=[], floor_ton=10.0)
    assert "bid_support" in metrics.missing


# --- Вчерашний флор -------------------------------------------------------


async def test_previous_day_floor_enables_the_falling_knife_filter(db):
    """Фильтр обвала сравнивает с флором суток назад.

    Своя история начинается с первого запуска, поэтому в первые сутки
    сравнивать было не с чем и фильтр молча пропускал падение.
    """
    async with session_scope() as session:
        assert await repo.seed_previous_day_floors(
            session, "mrkt", {"Lush Bouquet": 12.0}
        ) == 1

    async with session_scope() as session:
        assert await repo.floor_at(session, "Lush Bouquet", hours_ago=24) == pytest.approx(12.0)


async def test_own_history_wins_over_seeding(db):
    """Накопив свои наблюдения, площадку об этом больше не спрашиваем."""
    from app.storage.models import FloorSnapshot

    async with session_scope() as session:
        session.add(
            FloorSnapshot(
                market="mrkt",
                collection="Lush Bouquet",
                floor_ton=11.0,
                captured_at=utcnow() - timedelta(hours=25),
            )
        )

    async with session_scope() as session:
        assert await repo.seed_previous_day_floors(
            session, "mrkt", {"Lush Bouquet": 99.0}
        ) == 0


async def test_zero_floor_is_not_seeded(db):
    async with session_scope() as session:
        assert await repo.seed_previous_day_floors(session, "mrkt", {"Dead": 0.0}) == 0
