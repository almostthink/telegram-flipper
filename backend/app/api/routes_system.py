"""Системные эндпоинты: состояние приложения и чтение/запись конфига."""

from __future__ import annotations

import sys
import time

from fastapi import APIRouter

from app import paths
from app.config import APP_VERSION, Settings, settings

router = APIRouter(tags=["system"])

_STARTED_AT = time.time()


@router.get("/health")
async def health() -> dict:
    """Пульс для UI: индикатор связи в шапке опрашивает этот эндпоинт."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_sec": round(time.time() - _STARTED_AT, 1),
        "paper_mode": settings.paper_mode,
        "auto_trade": settings.auto_trade,
        "frozen": paths.is_frozen(),
        "python": sys.version.split()[0],
    }


@router.get("/config")
async def get_config() -> Settings:
    return settings


@router.put("/config")
async def update_config(patch: dict) -> Settings:
    """Частичное обновление конфига с сохранением на диск.

    Автомат не включаем, пока не выключен paper-режим и пуст whitelist —
    иначе получится «включил тумблер, а он торгует всем подряд».
    """
    merged = settings.model_dump()
    for key, value in patch.items():
        if key not in merged:
            continue
        if isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value

    updated = Settings(**merged)
    if updated.auto_trade and (updated.paper_mode or not updated.risk.collection_whitelist):
        updated.auto_trade = False

    interval_changed = updated.watch_interval_sec != settings.watch_interval_sec
    orders_changed = updated.orders.interval_sec != settings.orders.interval_sec

    for field in updated.model_fields:
        setattr(settings, field, getattr(updated, field))
    settings.save()

    if interval_changed:
        # Иначе новое значение легло бы в конфиг и не подействовало
        # до перезапуска приложения.
        from app import scheduler

        scheduler.reschedule_watch(settings.watch_interval_sec)

    if orders_changed:
        from app import scheduler

        scheduler.reschedule_orders(settings.orders.interval_sec)

    return settings


@router.get("/status")
async def status() -> dict:
    """Сводка для обзорной страницы."""
    from sqlalchemy import func, select

    from app.analytics import stats as stats_mod
    from app.api.deps import get_engine
    from app.auth.tma import auth
    from app.domain import Market
    from app.storage.db import session_scope
    from app.storage.models import FloorSnapshot, SaleRecord, SignalRecord

    trading = await stats_mod.compute(paper=settings.paper_mode, days=30)

    async with session_scope() as session:
        pending = await session.scalar(
            select(func.count()).select_from(SignalRecord).where(SignalRecord.passed.is_(True))
        )
        collections = await session.scalar(
            select(func.count(func.distinct(FloorSnapshot.collection)))
        )
        sales = await session.scalar(select(func.count()).select_from(SaleRecord))

    engine = get_engine()
    disabled = engine.scanner.disabled_markets

    marketplaces = {}
    for name, cfg in settings.marketplaces.items():
        known = name in {m.value for m in Market}
        token = auth.state(Market(name)) if known else None
        marketplaces[name] = {
            "label": Market(name).label if known else name,
            "enabled": cfg.enabled,
            "trade_enabled": cfg.trade_enabled,
            "connected": token is not None and bool(token.value) and name not in disabled,
            "note": disabled.get(name)
            or ("токен не задан" if token is None else f"источник: {token.source}"),
        }

    return {
        "stage": "все этапы реализованы",
        # None означает «не измерен»: в бумажном режиме баланс не
        # запрашивается вовсе, а в живом мог не ответить запрос.
        "balance_ton": engine.wallet.total_ton,
        "open_positions": trading.open_positions,
        "realized_pnl_ton": round(trading.realized_pnl_ton, 3),
        "unrealized_pnl_ton": round(trading.unrealized_pnl_ton, 3),
        "invested_ton": round(trading.invested_ton, 3),
        "signals_pending": pending or 0,
        "data": {
            "collections_tracked": collections or 0,
            "sales_recorded": sales or 0,
        },
        "engine": engine.status(),
        "marketplaces": marketplaces,
    }
