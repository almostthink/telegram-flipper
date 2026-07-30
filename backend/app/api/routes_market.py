"""Рыночные данные: сигналы, коллекции, разбор конкретной коллекции."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.analytics import liquidity as liquidity_mod
from app.analytics import pipeline
from app.api.deps import get_engine
from app.config import settings
from app.domain import utcnow
from app.storage import repo
from app.storage.db import session_scope
from app.storage.models import AttributeFloorSnapshot, FloorSnapshot, SignalRecord

router = APIRouter(tags=["market"])


@router.get("/signals")
async def signals(
    only_passed: bool = Query(default=True),
    limit: int = Query(default=100, le=500),
) -> dict:
    """Актуальные сигналы. По умолчанию только прошедшие все фильтры."""
    async with session_scope() as session:
        query = select(SignalRecord).order_by(SignalRecord.score.desc()).limit(limit)
        if only_passed:
            query = query.where(SignalRecord.passed.is_(True))
        rows = list((await session.execute(query)).scalars())

        rejections = list(
            (
                await session.execute(
                    select(SignalRecord.reject_reason, func.count())
                    .where(SignalRecord.passed.is_(False))
                    .group_by(SignalRecord.reject_reason)
                    .order_by(func.count().desc())
                )
            ).all()
        )

    return {
        "signals": [_signal_dict(row) for row in rows],
        "rejections": {reason: count for reason, count in rejections if reason},
        "min_roi": settings.analytics.min_roi,
        "min_liquidity": settings.analytics.min_liquidity_score,
    }


@router.post("/signals/refresh")
async def refresh_signals() -> dict:
    """Пересчитать сигналы по уже собранным данным, без обращения к площадкам."""
    computed = await pipeline.evaluate_all(settings)
    await pipeline.persist(computed)
    get_engine().last_signals = computed
    return {
        "total": len(computed),
        "passed": sum(1 for item in computed if item.passed),
        "rejections": pipeline.group_rejections(computed),
    }


@router.get("/collections")
async def collections(limit: int = Query(default=60, le=200)) -> dict:
    """Список отслеживаемых коллекций с флором и объёмом."""
    cutoff = utcnow() - timedelta(hours=24)
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(
                        FloorSnapshot.collection,
                        func.min(FloorSnapshot.floor_ton),
                        func.max(FloorSnapshot.volume_24h_ton),
                        func.max(FloorSnapshot.sales_24h),
                        func.max(FloorSnapshot.listed_count),
                    )
                    .where(FloorSnapshot.captured_at >= cutoff)
                    .group_by(FloorSnapshot.collection)
                    .order_by(func.max(FloorSnapshot.volume_24h_ton).desc().nullslast())
                    .limit(limit)
                )
            ).all()
        )

    return {
        "collections": [
            {
                "collection": name,
                "floor_ton": floor,
                "volume_24h_ton": volume,
                "sales_24h": sales,
                "listed_count": listed,
            }
            for name, floor, volume, sales, listed in rows
        ]
    }


@router.get("/collections/{collection}")
async def collection_detail(collection: str) -> dict:
    """Полный разбор коллекции: флор, история, ликвидность, атрибуты, книга."""
    async with session_scope() as session:
        floor = await repo.latest_floor(session, collection)
        if floor is None:
            raise HTTPException(status_code=404, detail="Нет данных по коллекции")

        listings = await repo.active_listings(session, collection, limit=300)
        sales = await repo.recent_sales(session, collection, days=30)
        floor_24h = await repo.floor_at(session, collection, hours_ago=24)

        history = list(
            (
                await session.execute(
                    select(
                        func.strftime("%Y-%m-%d %H:00", FloorSnapshot.captured_at),
                        func.min(FloorSnapshot.floor_ton),
                    )
                    .where(
                        FloorSnapshot.collection == collection,
                        FloorSnapshot.captured_at >= utcnow() - timedelta(days=14),
                    )
                    .group_by(func.strftime("%Y-%m-%d %H:00", FloorSnapshot.captured_at))
                    .order_by(func.strftime("%Y-%m-%d %H:00", FloorSnapshot.captured_at))
                )
            ).all()
        )

        attributes = list(
            (
                await session.execute(
                    select(
                        AttributeFloorSnapshot.kind,
                        AttributeFloorSnapshot.name,
                        func.min(AttributeFloorSnapshot.floor_ton),
                        func.max(AttributeFloorSnapshot.rarity_permille),
                    )
                    .where(
                        AttributeFloorSnapshot.collection == collection,
                        AttributeFloorSnapshot.captured_at >= utcnow() - timedelta(hours=12),
                    )
                    .group_by(AttributeFloorSnapshot.kind, AttributeFloorSnapshot.name)
                    .order_by(func.min(AttributeFloorSnapshot.floor_ton).desc())
                )
            ).all()
        )

    metrics = liquidity_mod.compute(
        collection,
        sales=sales,
        listings=listings,
        floor_ton=floor,
        floor_24h_ago=floor_24h,
    )

    return {
        "collection": collection,
        "floor_ton": floor,
        "liquidity": {
            "score": metrics.score,
            "measured": metrics.is_measured,
            "missing": metrics.missing,
            "parts": metrics.parts,
            "sales_24h": metrics.sales_24h,
            "sales_7d": metrics.sales_7d,
            "tts_median_hours": metrics.tts_median_hours,
            "expected_tts_hours": round(metrics.expected_tts_hours, 1),
            "depth_10pct": metrics.depth_10pct,
            "floor_trend_24h": metrics.floor_trend_24h,
        },
        "floor_history": [{"t": moment, "floor_ton": value} for moment, value in history],
        "attributes": [
            {
                "kind": kind,
                "name": name,
                "floor_ton": attribute_floor,
                "rarity_permille": rarity,
                "multiplier": round(attribute_floor / floor, 2) if floor else None,
            }
            for kind, name, attribute_floor, rarity in attributes
        ],
        "book": [
            {
                "market": item.market,
                "listing_id": item.listing_id,
                "price_ton": item.price_ton,
                "model": item.model,
                "backdrop": item.backdrop,
                "symbol": item.symbol,
                "url": item.url,
            }
            for item in listings[:60]
        ],
        "recent_sales": [
            {
                "price_ton": sale.price_ton,
                "model": sale.model,
                "sold_at": sale.sold_at.isoformat(),
                "tts_hours": sale.tts_hours,
            }
            for sale in sales[:60]
        ],
    }


def _signal_dict(row: SignalRecord) -> dict:
    return {
        "id": row.id,
        "market": row.market,
        "listing_id": row.listing_id,
        "collection": row.collection,
        "number": row.number,
        "number_score": round(row.number_score or 0.0, 3),
        "number_label": row.number_label,
        "monochrome_score": row.monochrome_score,
        "model": row.model,
        "backdrop": row.backdrop,
        "symbol": row.symbol,
        "ask_ton": round(row.ask_ton, 3),
        "fair_value_ton": round(row.fair_value_ton, 3),
        "net_roi": round(row.net_roi, 4),
        "liquidity_score": round(row.liquidity_score, 3),
        "confidence": round(row.confidence, 3),
        "expected_tts_hours": (
            round(row.expected_tts_hours, 1) if row.expected_tts_hours else None
        ),
        "score": round(row.score, 5),
        "passed": row.passed,
        "reject_reason": row.reject_reason,
        "explanation": row.explanation,
        "created_at": row.created_at.isoformat(),
    }
