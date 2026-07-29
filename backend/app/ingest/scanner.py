"""Сбор данных с площадок по расписанию.

Две петли с разной частотой:

* **широкая** — раз в несколько минут собирает флоры всех коллекций.
  Дёшево по запросам, даёт тренд флора и список коллекций для слежения.
* **глубокая** — обходит топ коллекций по объёму и тянет листинги,
  флоры атрибутов, ленту сделок и офферы. Дорого, поэтому только по тем
  коллекциям, где есть ликвидность.

Сканер никогда не роняет приложение: ошибка одной площадки логируется
и не мешает остальным. Площадка с протухшим токеном временно исключается.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.adapters import registry
from app.adapters.base import AuthExpired, Marketplace, MarketplaceError
from app.auth.tma import auth
from app.config import Settings
from app.domain import Market
from app.storage import repo
from app.storage.db import session_scope

log = logging.getLogger(__name__)

#: Сколько коллекций обходить глубоко за один проход.
DEEP_SCAN_COLLECTIONS = 25
#: Максимум листингов на коллекцию — дальше цены уже не флип-зона.
LISTINGS_PER_COLLECTION = 100


@dataclass
class ScanStats:
    floors: int = 0
    listings: int = 0
    sales: int = 0
    attribute_floors: int = 0
    vanished: int = 0
    tts_recovered: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: ScanStats) -> None:
        self.floors += other.floors
        self.listings += other.listings
        self.sales += other.sales
        self.attribute_floors += other.attribute_floors
        self.vanished += other.vanished
        self.tts_recovered += other.tts_recovered
        self.errors.extend(other.errors)


class Scanner:
    """Опрос площадок и запись наблюдений в базу."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_stats = ScanStats()
        self._disabled: dict[Market, str] = {}

    # --- Широкая петля ---------------------------------------------------

    async def scan_floors(self) -> ScanStats:
        stats = ScanStats()
        adapters = registry.build_enabled(self.settings)

        async def one(market: Market, adapter: Marketplace) -> None:
            try:
                async with adapter:
                    floors = await adapter.collection_floors()
                async with session_scope() as session:
                    stats.floors += await repo.record_floors(session, floors)
                self._disabled.pop(market, None)
            except AuthExpired as exc:
                await self._handle_auth_expired(market, exc, stats)
            except MarketplaceError as exc:
                stats.errors.append(f"{market.value}: {exc}")
                log.warning("Сбор флоров %s не удался: %s", market.value, exc)

        await asyncio.gather(
            *(
                one(market, adapter)
                for market, adapter in adapters.items()
                if market not in self._disabled
            )
        )
        self.last_stats = stats
        return stats

    # --- Глубокая петля --------------------------------------------------

    async def scan_collections(self, collections: list[str] | None = None) -> ScanStats:
        stats = ScanStats()

        if collections is None:
            async with session_scope() as session:
                collections = await repo.tracked_collections(session, limit=DEEP_SCAN_COLLECTIONS)

        if not collections:
            log.info("Нет коллекций для глубокого сканирования — сначала соберите флоры")
            return stats

        adapters = registry.build_enabled(self.settings)
        for market, adapter in adapters.items():
            if market in self._disabled:
                continue
            try:
                async with adapter:
                    for collection in collections:
                        stats.merge(await self._scan_one(adapter, collection))
                self._disabled.pop(market, None)
            except AuthExpired as exc:
                await self._handle_auth_expired(market, exc, stats)
            except MarketplaceError as exc:
                stats.errors.append(f"{market.value}: {exc}")
                log.warning("Глубокое сканирование %s не удалось: %s", market.value, exc)

        self.last_stats = stats
        log.info(
            "Скан: листингов %d, продаж %d, флоров атрибутов %d, пропало %d, TTS %d, ошибок %d",
            stats.listings, stats.sales, stats.attribute_floors,
            stats.vanished, stats.tts_recovered, len(stats.errors),
        )
        return stats

    async def _scan_one(self, adapter: Marketplace, collection: str) -> ScanStats:
        """Один проход по коллекции на одной площадке."""
        stats = ScanStats()

        try:
            listings = await adapter.listings(collection, limit=LISTINGS_PER_COLLECTION)
        except MarketplaceError as exc:
            stats.errors.append(f"{adapter.name.value}/{collection}: {exc}")
            return stats

        async with session_scope() as session:
            stats.listings += await repo.upsert_listings(session, listings)
            alive = {item.listing_id for item in listings}
            # Помечаем пропавшие только если выдача непустая: пустой ответ
            # чаще означает сбой запроса, чем распродажу всей коллекции.
            if alive:
                vanished = await repo.mark_gone(
                    session, adapter.name.value, collection, alive
                )
                stats.vanished += len(vanished)

        for coroutine, handler in (
            (adapter.activity(collection, limit=100), self._save_activity),
            (adapter.attribute_floors(collection), self._save_attribute_floors),
        ):
            try:
                payload = await coroutine
            except MarketplaceError as exc:
                log.debug("%s/%s: %s", adapter.name.value, collection, exc)
                continue
            await handler(payload, stats)

        async with session_scope() as session:
            stats.tts_recovered += await repo.attach_tts(
                session, adapter.name.value, collection
            )

        return stats

    async def _save_activity(self, events, stats: ScanStats) -> None:
        if not events:
            return
        async with session_scope() as session:
            stats.sales += await repo.record_sales(session, events)

    async def _save_attribute_floors(self, floors, stats: ScanStats) -> None:
        if not floors:
            return
        async with session_scope() as session:
            stats.attribute_floors += await repo.record_attribute_floors(session, floors)

    # --- Обработка авторизации ------------------------------------------

    async def _handle_auth_expired(
        self, market: Market, exc: Exception, stats: ScanStats
    ) -> None:
        """Пробуем обновить токен, иначе выключаем площадку до вмешательства."""
        log.warning("%s: %s — пробую обновить токен", market.value, exc)
        try:
            token = await auth.ensure_fresh(market)
        except Exception as refresh_exc:  # noqa: BLE001
            token = None
            log.warning("Обновление токена %s не удалось: %s", market.value, refresh_exc)

        if token:
            stats.errors.append(f"{market.value}: токен обновлён, повтор на следующем проходе")
            return

        self._disabled[market] = "требуется новый токен"
        stats.errors.append(f"{market.value}: нужен новый токен — задайте его в Настройках")

    @property
    def disabled_markets(self) -> dict[str, str]:
        return {market.value: reason for market, reason in self._disabled.items()}

    def reset_disabled(self) -> None:
        self._disabled.clear()
