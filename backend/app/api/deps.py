"""Общие зависимости API: единственный экземпляр движка на приложение."""

from __future__ import annotations

from app.config import settings
from app.trading.engine import TradingEngine

_engine: TradingEngine | None = None


def get_engine() -> TradingEngine:
    global _engine
    if _engine is None:
        _engine = TradingEngine(settings)
    return _engine


def reload_engine() -> TradingEngine:
    """Применяем изменённые настройки без перезапуска приложения."""
    engine = get_engine()
    engine.reload_settings(settings)
    return engine
