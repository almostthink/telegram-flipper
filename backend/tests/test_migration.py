"""Проверки достройки схемы при обновлении приложения.

База пользователя переживает обновления, а ``create_all`` создаёт только
отсутствующие таблицы и никогда не добавляет колонки. Без этой достройки
любое новое поле в модели роняло приложение с «no such column» на базе,
созданной прошлой версией.

Накопленная история — самый ценный актив: без неё не откалибровать модель
цены. Поэтому обновление обязано достраивать схему, а не требовать
удаления базы.
"""

from __future__ import annotations

import sqlite3

import pytest
from app.storage.db import dispose_db, init_db, session_scope
from app.storage.models import ListingSnapshot
from sqlalchemy import select

#: Схема listing_snapshots до появления номера выпуска и блокировки
#: перепродажи — ровно то, что лежит у пользователя прошлой версии.
LEGACY_SCHEMA = """
CREATE TABLE listing_snapshots (
  id INTEGER PRIMARY KEY,
  market VARCHAR(16),
  listing_id VARCHAR(128),
  collection VARCHAR(128),
  model VARCHAR(128),
  backdrop VARCHAR(128),
  symbol VARCHAR(128),
  model_rarity FLOAT,
  backdrop_rarity FLOAT,
  symbol_rarity FLOAT,
  price_ton FLOAT,
  seller VARCHAR(128),
  listed_at DATETIME,
  seen_at DATETIME,
  gone_at DATETIME,
  url TEXT
);
INSERT INTO listing_snapshots
  (id, market, listing_id, collection, model, price_ton, seen_at)
VALUES (1, 'mrkt', 'L1', 'Evil Eye', 'Monochrome', 7.8, datetime('now'));
"""


@pytest.fixture
async def legacy_db(tmp_path, monkeypatch):
    """Подсовываем приложению базу, созданную прошлой версией."""
    from app import paths

    await dispose_db()
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    connection = sqlite3.connect(tmp_path / "flipper.db")
    connection.executescript(LEGACY_SCHEMA)
    connection.commit()
    connection.close()

    yield tmp_path
    await dispose_db()


async def test_missing_columns_are_added(legacy_db):
    await init_db()

    connection = sqlite3.connect(legacy_db / "flipper.db")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(listing_snapshots)")}
    connection.close()

    for expected in ("number", "resale_available_at", "locked"):
        assert expected in columns, f"колонка {expected} не добавлена"


async def test_existing_rows_survive_migration(legacy_db):
    """Данные терять нельзя — история и есть обучающая выборка модели."""
    await init_db()

    async with session_scope() as session:
        rows = list((await session.execute(select(ListingSnapshot))).scalars())

    assert len(rows) == 1
    row = rows[0]
    assert row.listing_id == "L1"
    assert row.collection == "Evil Eye"
    assert row.price_ton == 7.8
    # Новые поля у старых строк пустые, и это корректно.
    assert row.number is None
    assert row.resale_available_at is None


async def test_queries_work_after_migration(legacy_db):
    """Именно этот запрос падал с 500 на странице «Рынок»."""
    from app.storage import repo

    await init_db()
    async with session_scope() as session:
        listings = await repo.active_listings(session, "Evil Eye")

    assert len(listings) == 1


async def test_migration_is_idempotent(legacy_db):
    """Повторный запуск не должен ничего ломать."""
    await init_db()
    await init_db()

    async with session_scope() as session:
        rows = list((await session.execute(select(ListingSnapshot))).scalars())
    assert len(rows) == 1


async def test_fresh_database_needs_no_migration(tmp_path, monkeypatch):
    from app import paths

    await dispose_db()
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    await init_db()
    async with session_scope() as session:
        assert list((await session.execute(select(ListingSnapshot))).scalars()) == []
    await dispose_db()
