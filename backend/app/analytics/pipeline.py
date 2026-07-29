"""Конвейер аналитики: из базы — в готовые сигналы.

Один проход: по каждой отслеживаемой коллекции собрать контекст,
прогнать антифрод, посчитать ликвидность и оценить все активные листинги.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import update

from app.analytics import antifraud
from app.analytics import liquidity as liquidity_mod
from app.analytics.pricing import PricingContext
from app.analytics.signals import EvaluationInput, Signal, evaluate_collection
from app.config import Settings
from app.storage import repo
from app.storage.db import session_scope
from app.storage.models import ListingSnapshot, SaleRecord

log = logging.getLogger(__name__)


async def evaluate_all(
    settings: Settings, *, collections: list[str] | None = None, top_n: int = 40
) -> list[Signal]:
    """Считаем сигналы по всем отслеживаемым коллекциям."""
    async with session_scope() as session:
        if collections is None:
            collections = await repo.tracked_collections(session, limit=top_n)

    signals: list[Signal] = []
    for collection in collections:
        try:
            signals.extend(await evaluate_one(settings, collection))
        except Exception:  # noqa: BLE001 — одна коллекция не должна ронять проход
            log.exception("Ошибка оценки коллекции %s", collection)

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals


async def evaluate_one(settings: Settings, collection: str) -> list[Signal]:
    async with session_scope() as session:
        floor = await repo.latest_floor(session, collection)
        if not floor or floor <= 0:
            return []

        listings = await repo.active_listings(session, collection)
        if not listings:
            return []

        sales = await repo.recent_sales(session, collection, days=30, include_suspicious=True)

        # Антифрод прогоняем до расчёта: помеченные сделки не должны
        # попасть ни в регрессию, ни в метрики ликвидности.
        report = antifraud.screen_sales(sales)
        if report.suspicious_sale_ids:
            await session.execute(
                update(SaleRecord)
                .where(SaleRecord.id.in_(report.suspicious_sale_ids))
                .values(suspicious=True)
            )
            for sale in sales:
                if sale.id in report.suspicious_sale_ids:
                    sale.suspicious = True

        floor_24h_ago = await repo.floor_at(session, collection, hours_ago=24)
        attribute_floors = await repo.attribute_floor_map(session, collection)

    clean_sales = [sale for sale in sales if not sale.suspicious]

    metrics = liquidity_mod.compute(
        collection,
        sales=clean_sales,
        listings=listings,
        floor_ton=floor,
        floor_24h_ago=floor_24h_ago,
    )

    context = PricingContext(
        collection=collection,
        floor_ton=floor,
        attribute_floors=attribute_floors,
        sales=clean_sales,
        min_samples=settings.analytics.min_samples_for_regression,
        halflife_days=settings.analytics.history_halflife_days,
        number_bonus=settings.collectible.number_bonus,
        preferred_backdrops=settings.collectible.preferred_backdrops,
        backdrop_bonus=settings.collectible.backdrop_bonus,
    )

    tradable = [
        listing
        for listing in listings
        if (cfg := settings.marketplaces.get(listing.market)) and cfg.trade_enabled
    ]
    if not tradable:
        return []

    data = EvaluationInput(
        collection=collection,
        listings=tradable,
        context=context,
        liquidity=metrics,
        fee_sell_by_market={
            name: cfg.fee_sell for name, cfg in settings.marketplaces.items()
        },
        fee_buy_by_market={name: cfg.fee_buy for name, cfg in settings.marketplaces.items()},
        cross_market_floor=_reference_floor(listings, settings),
        max_position_ton=settings.risk.max_position_ton,
    )

    return evaluate_collection(data, settings.analytics, settings.collectible)


def _reference_floor(listings: list[ListingSnapshot], settings: Settings) -> float | None:
    """Минимальная цена на площадках, где мы не торгуем.

    Если Portals и GetGems показывают тот же уровень, что и «дешёвый» лот
    на MRKT, — это не скидка, а текущая цена рынка.
    """
    reference_prices: list[float] = []
    for listing in listings:
        cfg = settings.marketplaces.get(listing.market)
        if cfg and cfg.enabled and not cfg.trade_enabled and listing.price_ton > 0:
            reference_prices.append(listing.price_ton)
    return min(reference_prices) if reference_prices else None


async def persist(signals: list[Signal], *, keep_rejected: int = 200) -> int:
    """Сохраняем сигналы.

    Отклонённые тоже пишем, но с ограничением: по ним видно, правильные ли
    пороги. Все подряд хранить незачем — их тысячи за проход.
    """
    passed = [signal for signal in signals if signal.passed]
    rejected = [signal for signal in signals if not signal.passed]

    # Из отклонённых оставляем самые близкие к прохождению — именно они
    # показывают, где проходит граница.
    rejected.sort(key=lambda s: s.net_roi, reverse=True)
    selected = passed + rejected[:keep_rejected]

    async with session_scope() as session:
        await repo.purge_signals(session, older_than_hours=6)
        await repo.save_signals(session, [signal.to_record() for signal in selected])

    return len(selected)


def group_rejections(signals: list[Signal]) -> dict[str, int]:
    """Статистика отказов — что именно отсекает большинство лотов."""
    counts: dict[str, int] = defaultdict(int)
    for signal in signals:
        if signal.reject_reason:
            counts[signal.reject_reason.value] += 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
