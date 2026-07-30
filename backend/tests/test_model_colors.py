"""Добыча и хранение цвета модели.

Цвет модели не отдаёт ни одна площадка. Он достаётся разбором её
Lottie-анимации, стоит запроса к хранилищу и потому кэшируется навсегда:
внешность модели не меняется.
"""

from __future__ import annotations

import pytest
from app.adapters.base import MarketplaceError
from app.domain import Market
from app.ingest import model_colors
from app.storage.db import dispose_db, init_db, session_scope
from app.storage.models import ListingSnapshot, ModelColor
from sqlalchemy import select


@pytest.fixture
async def db(tmp_path, monkeypatch):
    from app import paths

    await dispose_db()
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    await init_db()
    yield
    await dispose_db()


class FakeAdapter:
    """Хранилище без сети: отдаёт заранее заданные анимации."""

    name = Market.MRKT

    def __init__(self, assets: dict[str, object], *, fail: bool = False) -> None:
        self.assets = assets
        self.fail = fail
        self.requested: list[str] = []

    async def fetch_asset(self, key: str):
        self.requested.append(key)
        if self.fail:
            raise MarketplaceError("хранилище недоступно")
        return self.assets.get(key)


def lottie(red: float, green: float, blue: float) -> dict:
    return {
        "layers": [{"shapes": [{"ty": "fl", "c": {"a": 0, "k": [red, green, blue, 1]}}]}]
    }


async def add_listing(collection: str, model: str) -> None:
    async with session_scope() as session:
        session.add(
            ListingSnapshot(
                market="mrkt",
                listing_id=f"{collection}-{model}",
                collection=collection,
                model=model,
                price_ton=10.0,
            )
        )


# --- Ключ анимации --------------------------------------------------------


def test_sticker_key_matches_the_real_format():
    """Подтверждено записью: hex от «Коллекция_Модель»."""
    key = model_colors.sticker_key("Evil Eye", "Monochrome")
    assert key == "gifts/stickers/4576696c204579655f4d6f6e6f6368726f6d65.json"


def test_key_survives_non_latin_names():
    key = model_colors.sticker_key("Тест", "Модель")
    assert key.startswith("gifts/stickers/") and key.endswith(".json")


# --- Разбор и хранение ----------------------------------------------------


async def test_color_is_resolved_and_stored(db):
    await add_listing("Lush Bouquet", "Cabbage")
    key = model_colors.sticker_key("Lush Bouquet", "Cabbage")
    adapter = FakeAdapter({key: lottie(0.2, 0.7, 0.3)})

    assert await model_colors.resolve(adapter, "Lush Bouquet", ["Cabbage"]) == 1

    palette = await model_colors.known_colors("Lush Bouquet")
    assert "Cabbage" in palette
    green = palette["Cabbage"]
    assert (green >> 8) & 0xFF > (green >> 16) & 0xFF, "зелёная составляющая больше красной"


async def test_resolved_model_is_not_requested_again(db):
    """Внешность модели не меняется — второй запрос был бы впустую."""
    await add_listing("Lush Bouquet", "Cabbage")
    key = model_colors.sticker_key("Lush Bouquet", "Cabbage")
    adapter = FakeAdapter({key: lottie(0.2, 0.7, 0.3)})
    await model_colors.resolve(adapter, "Lush Bouquet", ["Cabbage"])

    assert await model_colors.pending_models("Lush Bouquet") == []


async def test_only_models_seen_in_listings_are_resolved(db):
    """Разбирать модели, которых нет в продаже, незачем."""
    await add_listing("Lush Bouquet", "Cabbage")
    assert await model_colors.pending_models("Lush Bouquet") == ["Cabbage"]
    assert await model_colors.pending_models("Другая коллекция") == []


async def test_failure_is_remembered_and_retried_a_few_times(db):
    """Без учёта неудач каждый проход ходил бы за одним и тем же файлом."""
    await add_listing("Lush Bouquet", "Cabbage")
    adapter = FakeAdapter({}, fail=True)

    for _ in range(model_colors.MAX_ATTEMPTS):
        pending = await model_colors.pending_models("Lush Bouquet")
        assert pending == ["Cabbage"]
        await model_colors.resolve(adapter, "Lush Bouquet", pending)

    assert await model_colors.pending_models("Lush Bouquet") == []
    assert len(adapter.requested) == model_colors.MAX_ATTEMPTS


async def test_failure_reason_is_kept_for_diagnosis(db):
    await add_listing("Lush Bouquet", "Cabbage")
    await model_colors.resolve(FakeAdapter({}, fail=True), "Lush Bouquet", ["Cabbage"])

    async with session_scope() as session:
        row = (await session.execute(select(ModelColor))).scalar()

    assert row is not None
    assert row.rgb is None
    assert row.note and "недоступно" in row.note


async def test_animation_without_fills_is_a_failure_not_a_color(db):
    await add_listing("Lush Bouquet", "Cabbage")
    key = model_colors.sticker_key("Lush Bouquet", "Cabbage")
    adapter = FakeAdapter({key: {"layers": []}})

    assert await model_colors.resolve(adapter, "Lush Bouquet", ["Cabbage"]) == 0
    assert await model_colors.known_colors("Lush Bouquet") == {}


async def test_missing_storage_setting_is_reported(db):
    """Адрес хранилища сбрасывали импортом HAR — причина должна быть видна."""
    await add_listing("Lush Bouquet", "Cabbage")
    adapter = FakeAdapter({})  # fetch_asset вернёт None

    await model_colors.resolve(adapter, "Lush Bouquet", ["Cabbage"])

    async with session_scope() as session:
        row = (await session.execute(select(ModelColor))).scalar()
    assert row is not None and row.note is not None


async def test_batch_is_bounded(db):
    """За проход разбираем понемногу, чтобы не упереться в лимит частоты."""
    for index in range(model_colors.RESOLVE_PER_SCAN + 5):
        await add_listing("Lush Bouquet", f"Model{index}")

    pending = await model_colors.pending_models("Lush Bouquet")
    assert len(pending) == model_colors.RESOLVE_PER_SCAN
