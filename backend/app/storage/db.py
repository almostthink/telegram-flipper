"""Подключение к SQLite и создание схемы."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import paths
from app.storage.models import Base

log = logging.getLogger(__name__)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        url = f"sqlite+aiosqlite:///{paths.db_path()}"
        _engine = create_async_engine(url, echo=False, future=True)

        @event.listens_for(_engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            # WAL: сканер пишет, а UI читает одновременно и не блокируется.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с автоматическим commit при успехе и rollback при ошибке."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("База готова: %s", paths.db_path())


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def vacuum_old_data(keep_days: int = 60) -> int:
    """Чистим устаревшие снимки, чтобы база не росла бесконечно.

    Продажи не трогаем: это обучающая выборка модели, и она ценна целиком.
    """
    cutoff = f"datetime('now', '-{int(keep_days)} days')"
    removed = 0
    async with session_scope() as session:
        for table in ("floor_snapshots", "attribute_floors"):
            result = await session.execute(
                text(f"DELETE FROM {table} WHERE captured_at < {cutoff}")  # noqa: S608
            )
            removed += result.rowcount or 0
        result = await session.execute(
            text(f"DELETE FROM listing_snapshots WHERE gone_at IS NOT NULL AND gone_at < {cutoff}")  # noqa: S608
        )
        removed += result.rowcount or 0
    if removed:
        log.info("Удалено устаревших записей: %d", removed)
    return removed
