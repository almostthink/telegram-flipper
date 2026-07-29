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

    for field in updated.model_fields:
        setattr(settings, field, getattr(updated, field))
    settings.save()
    return settings


@router.get("/status")
async def status() -> dict:
    """Сводка для Dashboard. На Этапе 0 — заглушки с честными нулями."""
    return {
        "stage": "0 — скелет приложения",
        "balance_ton": None,
        "open_positions": 0,
        "realized_pnl_ton": 0.0,
        "unrealized_pnl_ton": 0.0,
        "signals_pending": 0,
        "marketplaces": {
            name: {
                "enabled": cfg.enabled,
                "trade_enabled": cfg.trade_enabled,
                "connected": False,
                "note": "адаптер подключается на Этапе 1",
            }
            for name, cfg in settings.marketplaces.items()
        },
    }
