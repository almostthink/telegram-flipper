"""Бэктест стратегии на собранной истории.

Прогоняет прошлые листинги через текущие пороги и смотрит, что случилось
с этими подарками дальше. Отвечает на вопрос «а если бы я торговал по
этим настройкам последний месяц».

Честные ограничения, о которых нужно помнить:

* **Выживаемость данных.** История есть только по тем коллекциям, которые
  приложение уже сканировало. Чем короче наблюдение, тем оптимистичнее
  результат.
* **Нет конкуренции.** В реальности дешёвый лот могли забрать за секунды.
  Бэктест считает, что он достался нам.
* **Продажа моделируется по реальным сделкам**, но очередь продавцов не
  учитывается — фактический TTS будет хуже.

Поэтому результат бэктеста — верхняя граница, а не прогноз.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from sqlalchemy import select

from app.analytics import liquidity as liquidity_mod
from app.analytics import pricing
from app.analytics.pricing import PricingContext
from app.analytics.signals import listing_to_gift
from app.config import Settings
from app.domain import utcnow
from app.storage import repo
from app.storage.db import session_scope
from app.storage.models import ListingSnapshot, SaleRecord

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BacktestTrade:
    collection: str
    model: str | None
    buy_price_ton: float
    fair_value_ton: float
    exit_price_ton: float | None
    net_pnl_ton: float
    hold_hours: float | None
    outcome: str  # sold | stuck


@dataclass(slots=True)
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    candidates_considered: int = 0
    days: int = 30
    note: str = ""

    @property
    def net_pnl_ton(self) -> float:
        return sum(trade.net_pnl_ton for trade in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for trade in self.trades if trade.net_pnl_ton > 0) / len(self.trades)

    @property
    def stuck_share(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for trade in self.trades if trade.outcome == "stuck") / len(self.trades)

    def as_dict(self) -> dict:
        invested = sum(trade.buy_price_ton for trade in self.trades)
        return {
            "days": self.days,
            "candidates_considered": self.candidates_considered,
            "trades": len(self.trades),
            "net_pnl_ton": round(self.net_pnl_ton, 3),
            "invested_ton": round(invested, 3),
            "return_on_invested": round(self.net_pnl_ton / invested, 4) if invested else 0.0,
            "win_rate": round(self.win_rate, 4),
            "stuck_share": round(self.stuck_share, 4),
            "note": self.note,
            "sample": [
                {
                    "collection": trade.collection,
                    "model": trade.model,
                    "buy_ton": round(trade.buy_price_ton, 3),
                    "fair_ton": round(trade.fair_value_ton, 3),
                    "exit_ton": round(trade.exit_price_ton, 3) if trade.exit_price_ton else None,
                    "pnl_ton": round(trade.net_pnl_ton, 3),
                    "hold_hours": round(trade.hold_hours, 1) if trade.hold_hours else None,
                    "outcome": trade.outcome,
                }
                for trade in sorted(self.trades, key=lambda t: t.net_pnl_ton)[:50]
            ],
        }


async def run(settings: Settings, *, days: int = 30) -> BacktestResult:
    result = BacktestResult(days=days)
    since = utcnow() - timedelta(days=days)

    async with session_scope() as session:
        collections = await repo.tracked_collections(session, limit=50)

    if not collections:
        result.note = "Нет накопленной истории — дайте сканеру поработать хотя бы сутки."
        return result

    for collection in collections:
        try:
            await _backtest_collection(settings, collection, since, result)
        except Exception:  # noqa: BLE001
            log.exception("Бэктест коллекции %s не удался", collection)

    if not result.trades:
        result.note = (
            f"За {days} дн. ни один лот не прошёл пороги. "
            "Это нормально на короткой истории: ослабьте min_roi или дайте "
            "сканеру собрать больше данных."
        )
    else:
        result.note = (
            "Верхняя граница результата: конкуренция за лоты и очередь "
            "продавцов не моделируются."
        )
    return result


async def _backtest_collection(
    settings: Settings, collection: str, since, result: BacktestResult
) -> None:
    async with session_scope() as session:
        floor = await repo.latest_floor(session, collection)
        if not floor or floor <= 0:
            return

        # Кандидаты — лоты, которые уже исчезли из выдачи: только по ним
        # известно, чем всё закончилось.
        candidates = list(
            (
                await session.execute(
                    select(ListingSnapshot).where(
                        ListingSnapshot.collection == collection,
                        ListingSnapshot.gone_at.is_not(None),
                        ListingSnapshot.seen_at >= since,
                    )
                )
            ).scalars()
        )
        sales = await repo.recent_sales(session, collection, days=days_of(since))
        attribute_floors = await repo.attribute_floor_map(session, collection)
        active = await repo.active_listings(session, collection)

    if not candidates:
        return

    metrics = liquidity_mod.compute(
        collection, sales=sales, listings=active, floor_ton=floor
    )
    if metrics.score < settings.analytics.min_liquidity_score:
        return

    context = PricingContext(
        collection=collection,
        floor_ton=floor,
        attribute_floors=attribute_floors,
        sales=sales,
        min_samples=settings.analytics.min_samples_for_regression,
        halflife_days=settings.analytics.history_halflife_days,
    )

    for listing in candidates:
        cfg = settings.marketplaces.get(listing.market)
        if cfg is None or not cfg.trade_enabled:
            continue

        result.candidates_considered += 1
        gift = listing_to_gift(listing)
        fair = pricing.estimate(context, gift)
        if not fair.is_usable or fair.confidence < 0.25:
            continue

        roi = pricing.net_roi(
            listing.price_ton,
            fair.conservative_ton,
            fee_sell=cfg.fee_sell,
            fee_buy=cfg.fee_buy,
            gas_ton=settings.analytics.gas_ton,
        )
        if roi < settings.analytics.min_roi:
            continue
        if listing.price_ton > settings.risk.max_position_ton:
            continue

        result.trades.append(
            _simulate_exit(listing, fair.value_ton, sales, settings, cfg.fee_sell, cfg.fee_buy)
        )


def _simulate_exit(
    listing: ListingSnapshot,
    fair_value: float,
    sales: list[SaleRecord],
    settings: Settings,
    fee_sell: float,
    fee_buy: float,
) -> BacktestTrade:
    """Ищем в истории продажу похожего лота после нашей покупки."""
    bought_at = _aware(listing.gone_at or listing.seen_at)
    ask = fair_value * (1 + settings.sell.initial_markup)
    deadline = bought_at + timedelta(hours=settings.sell.max_hold_hours)
    gas = settings.analytics.gas_ton
    cost = listing.price_ton * (1 + fee_buy) + gas

    matches = sorted(
        (
            sale
            for sale in sales
            if not sale.suspicious
            and sale.model == listing.model
            and bought_at < _aware(sale.sold_at) <= deadline
        ),
        key=lambda sale: _aware(sale.sold_at),
    )

    # Лестница переоценки: с каждым интервалом наш аск ниже, поэтому
    # проверяем, догнала ли рыночная цена наш текущий аск.
    for sale in matches:
        hours = (_aware(sale.sold_at) - bought_at).total_seconds() / 3600
        steps = int(hours // settings.sell.reprice_interval_hours)
        current_ask = ask * ((1 - settings.sell.reprice_step) ** steps)
        if sale.price_ton >= current_ask:
            proceeds = current_ask * (1 - fee_sell) - gas
            return BacktestTrade(
                collection=listing.collection,
                model=listing.model,
                buy_price_ton=listing.price_ton,
                fair_value_ton=fair_value,
                exit_price_ton=current_ask,
                net_pnl_ton=proceeds - cost,
                hold_hours=hours,
                outcome="sold",
            )

    # Не продалось за max_hold — сбрасываем с дисконтом, как это сделал бы
    # движок. Это и есть цена зависшей позиции.
    dump_price = listing.price_ton * 0.92
    proceeds = dump_price * (1 - fee_sell) - gas
    return BacktestTrade(
        collection=listing.collection,
        model=listing.model,
        buy_price_ton=listing.price_ton,
        fair_value_ton=fair_value,
        exit_price_ton=dump_price,
        net_pnl_ton=proceeds - cost,
        hold_hours=settings.sell.max_hold_hours,
        outcome="stuck",
    )


def days_of(since) -> int:
    return max(int((utcnow() - _aware(since)).total_seconds() / 86400), 1)


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)
