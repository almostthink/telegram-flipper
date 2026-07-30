"""Добыча цвета модели и его хранение.

Цвет фона площадка отдаёт числом прямо в карточке лота. Цвет модели не
отдаёт никто — но ``modelStickerKey`` указывает на Lottie-анимацию, и
цвета лежат в ней явно. Отсюда и берём.

Работа стоит запроса к хранилищу, поэтому результат кэшируется навсегда:
внешность модели не меняется. Разбираем понемногу за проход — цвет нужен
для надбавки к оценке, а не для решения прямо сейчас, и ради него не
стоит снова упираться в лимит частоты.

Неудачи тоже запоминаются, с ограничением на число попыток. Иначе каждый
проход ходил бы за одним и тем же недоступным файлом.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.adapters.base import Marketplace, MarketplaceError
from app.analytics.lottie import dominant_color
from app.domain import utcnow
from app.storage.db import session_scope
from app.storage.models import ListingSnapshot, ModelColor

log = logging.getLogger(__name__)

#: Сколько моделей разбирать за один проход. Модели в коллекции
#: исчисляются десятками и разбираются один раз за всё время, так что
#: полный охват набирается за несколько циклов.
RESOLVE_PER_SCAN = 8

#: Сколько раз пробовать, прежде чем оставить модель без цвета. Причина
#: обычно постоянная — сменившийся адрес хранилища, — и повторять
#: бесконечно бессмысленно.
MAX_ATTEMPTS = 3


def sticker_key(collection: str, model: str) -> str:
    """Ключ анимации модели в хранилище площадки.

    Подтверждено записью: ключ — это шестнадцатеричная запись строки
    ``Коллекция_Модель``. Например ``Evil Eye_Monochrome`` превращается в
    ``4576696c204579655f4d6f6e6f6368726f6d65``.
    """
    return f"gifts/stickers/{f'{collection}_{model}'.encode().hex()}.json"


async def known_colors(collection: str) -> dict[str, int]:
    """Цвета моделей коллекции: имя модели → 0xRRGGBB."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ModelColor.model, ModelColor.rgb).where(
                    ModelColor.collection == collection,
                    ModelColor.rgb.is_not(None),
                )
            )
        ).all()
    return {model: rgb for model, rgb in rows}


async def pending_models(collection: str, limit: int = RESOLVE_PER_SCAN) -> list[str]:
    """Модели коллекции, чей цвет ещё не добыт.

    Берём из наблюдавшихся лотов: разбирать модели, которых нет в
    продаже, незачем.
    """
    async with session_scope() as session:
        seen = set(
            (
                await session.execute(
                    select(ListingSnapshot.model)
                    .where(
                        ListingSnapshot.collection == collection,
                        ListingSnapshot.model.is_not(None),
                    )
                    .distinct()
                )
            ).scalars()
        )
        settled = set(
            (
                await session.execute(
                    select(ModelColor.model).where(
                        ModelColor.collection == collection,
                        # Исчерпавшие попытки больше не трогаем.
                        (ModelColor.rgb.is_not(None)) | (ModelColor.attempts >= MAX_ATTEMPTS),
                    )
                )
            ).scalars()
        )

    return sorted(seen - settled)[:limit]


async def resolve(adapter: Marketplace, collection: str, models: list[str]) -> int:
    """Достаём и сохраняем цвета перечисленных моделей. Возвращаем сколько вышло."""
    resolved = 0
    for model in models:
        rgb, note = await _resolve_one(adapter, collection, model)
        await _store(collection, model, rgb, note)
        if rgb is not None:
            resolved += 1
    return resolved


async def _resolve_one(
    adapter: Marketplace, collection: str, model: str
) -> tuple[int | None, str | None]:
    try:
        document = await adapter.fetch_asset(sticker_key(collection, model))
    except MarketplaceError as exc:
        return None, str(exc)[:200]

    if document is None:
        return None, "хранилище не настроено"

    color = dominant_color(document)
    if color is None:
        return None, "в анимации не нашлось заливок"
    return color.as_int, None


async def _store(collection: str, model: str, rgb: int | None, note: str | None) -> None:
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(ModelColor).where(
                    ModelColor.collection == collection, ModelColor.model == model
                )
            )
        ).scalar()

        if existing is None:
            session.add(
                ModelColor(
                    collection=collection,
                    model=model,
                    rgb=rgb,
                    attempts=0 if rgb is not None else 1,
                    note=note,
                    resolved_at=utcnow(),
                )
            )
            return

        existing.rgb = rgb
        existing.note = note
        existing.resolved_at = utcnow()
        if rgb is None:
            existing.attempts += 1
