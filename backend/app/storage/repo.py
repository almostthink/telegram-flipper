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
    ListingEvent,
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


async def known_listing_ids(
    session: AsyncSession, market: str, listing_ids: list[str]
) -> set[str]:
    """Какие из этих лотов мы уже видели.

    Быстрая петля обязана отличать новое предложение от повторно
    показанного: без этого она переоценивала бы одни и те же лоты каждые
    несколько секунд и упиралась бы в них вместо реакции на свежие.
    """
    if not listing_ids:
        return set()
    query = select(ListingSnapshot.listing_id).where(
        ListingSnapshot.market == market,
        ListingSnapshot.listing_id.in_(listing_ids),
    )
    return set((await session.execute(query)).scalars())


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


async def record_listing_events(session: AsyncSession, events: list[ActivityEvent]) -> int:
    """Пишем моменты выставления лотов из ленты событий.

    Нужны только для времени до продажи: продажа того же подарка минус его
    листинг и есть TTS, измеренный, а не восстановленный по совпадению цены.
    """
    rows = []
    seen: set[tuple[str, str]] = set()
    # Лента идёт от свежих к старым, поэтому первое вхождение подарка —
    # его последнее выставление. Более старые события затирать нечем.
    for event in events:
        if event.kind is not ActivityKind.LISTING or not event.gift.external_id:
            continue
        key = (event.market.value, event.gift.external_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "market": event.market.value,
                "gift_external_id": event.gift.external_id,
                "collection": event.gift.collection,
                "price_ton": event.price_ton,
                "listed_at": event.happened_at,
            }
        )

    if not rows:
        return 0

    statement = sqlite_insert(ListingEvent).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[ListingEvent.market, ListingEvent.gift_external_id],
        set_={
            "listed_at": statement.excluded.listed_at,
            "price_ton": statement.excluded.price_ton,
        },
        # Перевыставление сдвигает отсчёт вперёд; событие старее записанного
        # означает, что мы уже видели более свежее — трогать не надо.
        where=ListingEvent.listed_at < statement.excluded.listed_at,
    )
    result = await session.execute(statement)
    return result.rowcount or 0


async def attach_tts(session: AsyncSession, market: str, collection: str) -> int:
    """Восстанавливаем время до продажи, сопоставляя продажи с листингами.

    Прямого поля TTS у площадок нет. Считаем двумя способами, в порядке
    убывания точности:

    1. По событиям ленты — тот же экземпляр подарка выставлен, потом продан.
       Это измерение: обе даты относятся к одному предмету.
    2. По снимкам книги — совпадение коллекции, площадки и цены. Это
       догадка: в ликвидной коллекции цены повторяются, и сопоставиться
       может чужой лот. Остаётся для продаж, к которым события листинга
       не нашлось: у Portals лента короче, и без запасного способа часть
       сделок осталась бы без TTS вовсе.
    """
    updated = await _attach_tts_by_instance(session, market, collection)
    return updated + await _attach_tts_by_price(session, market, collection)


async def _attach_tts_by_instance(session: AsyncSession, market: str, collection: str) -> int:
    query = (
        select(SaleRecord, ListingEvent)
        .join(
            ListingEvent,
            (ListingEvent.market == SaleRecord.market)
            & (ListingEvent.gift_external_id == SaleRecord.gift_external_id),
        )
        .where(
            SaleRecord.market == market,
            SaleRecord.collection == collection,
            SaleRecord.tts_hours.is_(None),
            SaleRecord.gift_external_id.is_not(None),
        )
        .limit(500)
    )

    updated = 0
    for sale, listing in (await session.execute(query)).all():
        hours = _hours_between(listing.listed_at, sale.sold_at)
        if hours is None:
            continue
        sale.tts_hours = hours
        updated += 1
    return updated


async def _attach_tts_by_price(session: AsyncSession, market: str, collection: str) -> int:
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
        hours = _hours_between(listing.listed_at, sale.sold_at)
        if hours is None:
            continue
        sale.tts_hours = hours
        updated += 1

    return updated


def _hours_between(listed_at: datetime | None, sold_at: datetime | None) -> float | None:
    """Часы от листинга до продажи, если пара вообще осмысленна."""
    listed = _aware(listed_at)
    sold = _aware(sold_at)
    if listed is None or sold is None or sold <= listed:
        return None
    hours = (sold - listed).total_seconds() / 3600
    # Больше месяца — почти наверняка ошибка сопоставления.
    return None if hours > 24 * 30 else hours


async def record_floors(
    session: AsyncSession,
    floors: list[CollectionFloor],
    *,
    offers: dict[str, float] | None = None,
) -> int:
    """Пишем флоры, а вместе с ними верхние заявки на покупку.

    Заявки приходят отдельным запросом, но снимаются тем же проходом и
    имеют смысл только рядом с флором — отставание бида от флора и есть
    опора спроса.
    """
    if not floors:
        return 0
    best = offers or {}
    session.add_all(
        [
            FloorSnapshot(
                market=item.market.value,
                collection=item.collection,
                floor_ton=item.floor_ton,
                volume_24h_ton=item.volume_24h_ton,
                sales_24h=item.sales_24h,
                listed_count=item.listed_count,
                best_offer_ton=best.get(item.collection),
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


async def seed_previous_day_floors(
    session: AsyncSession, market: str, floors: dict[str, float]
) -> int:
    """Записываем вчерашние флоры, которые площадка отдаёт сама.

    Своя история начинается с первого запуска, а фильтр «падающего ножа»
    сравнивает текущий флор со вчерашним. Без этого в первые сутки работы
    он не срабатывает вовсе: сравнивать не с чем, и приложение молча
    пропускает обвал.

    Это не выдумка задним числом: площадка сообщает измеренное значение,
    и мы кладём его на тот момент, к которому оно относится. Сеяние само
    прекращается, как только накопится своя история.
    """
    if not floors:
        return 0

    moment = utcnow() - timedelta(hours=24)
    # Если по коллекции уже есть наблюдение той поры — своё, и оно точнее.
    covered = set(
        (
            await session.execute(
                select(FloorSnapshot.collection).where(
                    FloorSnapshot.market == market,
                    FloorSnapshot.collection.in_(list(floors)),
                    FloorSnapshot.captured_at <= utcnow() - timedelta(hours=20),
                )
            )
        ).scalars()
    )

    rows = [
        FloorSnapshot(
            market=market,
            collection=collection,
            floor_ton=value,
            captured_at=moment,
        )
        for collection, value in floors.items()
        if collection not in covered and value > 0
    ]
    session.add_all(rows)
    return len(rows)


async def best_offer(session: AsyncSession, collection: str) -> float | None:
    """Лучшая заявка на покупку за последний час.

    Максимум по площадкам: выходить будем туда, где дают больше. None
    означает «данных нет» — и это не то же самое, что «спроса нет»:
    ликвидность в первом случае исключает компонент, а не обнуляет его.
    """
    cutoff = utcnow() - timedelta(hours=1)
    query = select(func.max(FloorSnapshot.best_offer_ton)).where(
        FloorSnapshot.collection == collection,
        FloorSnapshot.captured_at >= cutoff,
        FloorSnapshot.best_offer_ton.is_not(None),
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
