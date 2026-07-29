"""Операции с данными: запись наблюдений и выборки для аналитики."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import (
    ActivityEvent,
    ActivityKind,
    AttributeFloor,
    CollectionFloor,
    Gift,
    Listing,
    utcnow,
)
from app.storage.models import (
    AttributeFloorSnapshot,
    FloorSnapshot,
    ListingSnapshot,
    SaleRecord,
    SignalRecord,
)

log = logging.getLogger(__name__)


def _gift_columns(gift: Gift) -> dict[str, object]:
    return {
        "collection": gift.collection,
        "number": gift.number,
        "model": gift.model.name if gift.model else None,
        "backdrop": gift.backdrop.name if gift.backdrop else None,
        "symbol": gift.symbol.name if gift.symbol else None,
        "model_rarity": gift.model.rarity_permille if gift.model else None,
        "backdrop_rarity": gift.backdrop.rarity_permille if gift.backdrop else None,
        "symbol_rarity": gift.symbol.rarity_permille if gift.symbol else None,
    }


# --- Запись наблюдений --------------------------------------------------


async def upsert_listings(session: AsyncSession, listings: list[Listing]) -> int:
    """Обновляем активные листинги. Повторное наблюдение обновляет цену и seen_at."""
    if not listings:
        return 0

    rows = [
        {
            "market": item.market.value,
            "listing_id": item.listing_id,
            **_gift_columns(item.gift),
            "price_ton": item.price_ton,
            "seller": item.seller,
            "listed_at": item.listed_at,
            "seen_at": item.seen_at,
            "gone_at": None,
            "url": item.url,
            "resale_available_at": item.resale_available_at,
            "locked": item.locked,
        }
        for item in listings
    ]

    statement = sqlite_insert(ListingSnapshot).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[ListingSnapshot.market, ListingSnapshot.listing_id],
        set_={
            "price_ton": statement.excluded.price_ton,
            "seen_at": statement.excluded.seen_at,
            "resale_available_at": statement.excluded.resale_available_at,
            "locked": statement.excluded.locked,
            # Лот вернулся в выдачу — снимаем отметку об исчезновении.
            "gone_at": None,
        },
    )
    await session.execute(statement)
    return len(rows)


async def mark_gone(
    session: AsyncSession, market: str, collection: str, alive_ids: set[str]
) -> list[ListingSnapshot]:
    """Помечаем пропавшие из выдачи листинги.

    Исчезновение означает продажу или снятие. Отличить одно от другого
    помогает лента сделок, но сам факт исчезновения — уже сигнал о
    скорости оборота коллекции.
    """
    query = select(ListingSnapshot).where(
        ListingSnapshot.market == market,
        ListingSnapshot.collection == collection,
        ListingSnapshot.gone_at.is_(None),
    )
    rows = list((await session.execute(query)).scalars())
    vanished = [row for row in rows if row.listing_id not in alive_ids]

    if vanished:
        now = utcnow()
        await session.execute(
            update(ListingSnapshot)
            .where(ListingSnapshot.id.in_([row.id for row in vanished]))
            .values(gone_at=now)
        )
    return vanished


async def record_sales(session: AsyncSession, events: list[ActivityEvent]) -> int:
    """Пишем продажи. Дубли отсекаются по (market, external_id)."""
    sales = [event for event in events if event.kind is ActivityKind.SALE]
    if not sales:
        return 0

    rows = []
    for event in sales:
        external_id = event.external_id or (
            f"{event.gift.external_id}:{int(event.happened_at.timestamp())}"
        )
        rows.append(
            {
                "market": event.market.value,
                "external_id": external_id,
                "gift_external_id": event.gift.external_id or None,
                **_gift_columns(event.gift),
                "price_ton": event.price_ton,
                "sold_at": event.happened_at,
                "buyer": event.buyer,
                "seller": event.seller,
            }
        )

    statement = sqlite_insert(SaleRecord).values(rows).on_conflict_do_nothing(
        index_elements=[SaleRecord.market, SaleRecord.external_id]
    )
    result = await session.execute(statement)
    return result.rowcount or 0


async def attach_tts(session: AsyncSession, market: str, collection: str) -> int:
    """Восстанавливаем время до продажи, сопоставляя продажи с листингами.

    Прямого поля TTS у площадок нет. Но если подарок наблюдался в листингах,
    а затем появился в ленте как проданный, разница между listed_at и sold_at
    и есть фактическое время до продажи. Это единственный способ измерить
    ликвидность честно, а не по обещаниям площадки.
    """
    query = (
        select(SaleRecord, ListingSnapshot)
        .join(
            ListingSnapshot,
            (ListingSnapshot.collection == SaleRecord.collection)
            & (ListingSnapshot.market == SaleRecord.market)
            & (ListingSnapshot.price_ton == SaleRecord.price_ton),
        )
        .where(
            SaleRecord.market == market,
            SaleRecord.collection == collection,
            SaleRecord.tts_hours.is_(None),
            ListingSnapshot.listed_at.is_not(None),
        )
        .limit(500)
    )

    updated = 0
    for sale, listing in (await session.execute(query)).all():
        listed_at = _aware(listing.listed_at)
        sold_at = _aware(sale.sold_at)
        if listed_at is None or sold_at is None or sold_at <= listed_at:
            continue
        hours = (sold_at - listed_at).total_seconds() / 3600
        # Больше месяца — почти наверняка ошибка сопоставления по цене.
        if hours > 24 * 30:
            continue
        sale.tts_hours = hours
        updated += 1

    return updated


async def record_floors(session: AsyncSession, floors: list[CollectionFloor]) -> int:
    if not floors:
        return 0
    session.add_all(
        [
            FloorSnapshot(
                market=item.market.value,
                collection=item.collection,
                floor_ton=item.floor_ton,
                volume_24h_ton=item.volume_24h_ton,
                sales_24h=item.sales_24h,
                listed_count=item.listed_count,
                captured_at=item.captured_at,
            )
            for item in floors
        ]
    )
    return len(floors)


async def record_attribute_floors(session: AsyncSession, floors: list[AttributeFloor]) -> int:
    if not floors:
        return 0
    session.add_all(
        [
            AttributeFloorSnapshot(
                market=item.market.value,
                collection=item.collection,
                kind=item.kind.value,
                name=item.name,
                floor_ton=item.floor_ton,
                rarity_permille=item.rarity_permille,
                captured_at=item.captured_at,
            )
            for item in floors
        ]
    )
    return len(floors)


# --- Выборки для аналитики ----------------------------------------------


async def recent_sales(
    session: AsyncSession,
    collection: str,
    *,
    days: int = 30,
    include_suspicious: bool = False,
) -> list[SaleRecord]:
    cutoff = utcnow() - timedelta(days=days)
    query = select(SaleRecord).where(
        SaleRecord.collection == collection, SaleRecord.sold_at >= cutoff
    )
    if not include_suspicious:
        query = query.where(SaleRecord.suspicious.is_(False))
    return list((await session.execute(query.order_by(SaleRecord.sold_at.desc()))).scalars())


async def latest_floor(session: AsyncSession, collection: str) -> float | None:
    """Минимальный флор по всем площадкам за последний час.

    Берём минимум, а не среднее: покупать мы будем там, где дешевле,
    и оценивать выход надо от реально достижимой цены.
    """
    cutoff = utcnow() - timedelta(hours=1)
    query = select(func.min(FloorSnapshot.floor_ton)).where(
        FloorSnapshot.collection == collection, FloorSnapshot.captured_at >= cutoff
    )
    return (await session.execute(query)).scalar()


async def floor_at(session: AsyncSession, collection: str, hours_ago: float) -> float | None:
    """Флор примерно N часов назад — для оценки тренда."""
    target = utcnow() - timedelta(hours=hours_ago)
    window = timedelta(hours=3)
    query = (
        select(func.min(FloorSnapshot.floor_ton))
        .where(
            FloorSnapshot.collection == collection,
            FloorSnapshot.captured_at >= target - window,
            FloorSnapshot.captured_at <= target + window,
        )
    )
    return (await session.execute(query)).scalar()


async def attribute_floor_map(
    session: AsyncSession, collection: str
) -> dict[tuple[str, str], float]:
    """Свежие флоры атрибутов: (kind, name) → floor_ton."""
    cutoff = utcnow() - timedelta(hours=6)
    query = (
        select(
            AttributeFloorSnapshot.kind,
            AttributeFloorSnapshot.name,
            func.min(AttributeFloorSnapshot.floor_ton),
        )
        .where(
            AttributeFloorSnapshot.collection == collection,
            AttributeFloorSnapshot.captured_at >= cutoff,
        )
        .group_by(AttributeFloorSnapshot.kind, AttributeFloorSnapshot.name)
    )
    return {(kind, name): floor for kind, name, floor in (await session.execute(query)).all()}


async def active_listings(
    session: AsyncSession, collection: str, *, limit: int = 500
) -> list[ListingSnapshot]:
    query = (
        select(ListingSnapshot)
        .where(ListingSnapshot.collection == collection, ListingSnapshot.gone_at.is_(None))
        .order_by(ListingSnapshot.price_ton)
        .limit(limit)
    )
    return list((await session.execute(query)).scalars())


async def tracked_collections(session: AsyncSession, *, limit: int = 200) -> list[str]:
    """Коллекции, по которым есть свежие данные, от больших объёмов к малым."""
    cutoff = utcnow() - timedelta(hours=24)
    query = (
        select(FloorSnapshot.collection, func.max(FloorSnapshot.volume_24h_ton).label("volume"))
        .where(FloorSnapshot.captured_at >= cutoff)
        .group_by(FloorSnapshot.collection)
        .order_by(func.max(FloorSnapshot.volume_24h_ton).desc().nullslast())
        .limit(limit)
    )
    return [row[0] for row in (await session.execute(query)).all()]


async def save_signals(session: AsyncSession, records: list[SignalRecord]) -> None:
    if records:
        session.add_all(records)


async def purge_signals(session: AsyncSession, older_than_hours: float = 6) -> int:
    """Сигналы живут недолго: цена лота меняется, старый сигнал вводит в заблуждение."""
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    result = await session.execute(
        delete(SignalRecord).where(
            SignalRecord.created_at < cutoff, SignalRecord.acted.is_(False)
        )
    )
    return result.rowcount or 0


def _aware(value: datetime | None) -> datetime | None:
    """SQLite отдаёт naive datetime — возвращаем в UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
