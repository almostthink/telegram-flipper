"""Статистика торговли для журнала.

Смысл раздела — заменить чужие цифры своими. Один прибыльный день ничего
не доказывает: при математическом ожидании 4% за сделку день «+11%»
случается примерно раз в четыре дня, и снять про него видео — не то же
самое, что заработать за месяц.

Поэтому считаем распределение по дням, а не только итог, и отдельно
разделяем реализованную прибыль и бумажную.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta

from sqlalchemy import select

from app.domain import utcnow
from app.storage.db import session_scope
from app.storage.models import Position


@dataclass(slots=True)
class DayStat:
    day: str
    realized_ton: float
    trades: int


@dataclass(slots=True)
class TradingStats:
    paper: bool
    closed_trades: int = 0
    open_positions: int = 0

    realized_pnl_ton: float = 0.0
    #: Бумажная переоценка открытых позиций. НЕ прибыль — до продажи и
    #: удержания комиссии она ничего не значит.
    unrealized_pnl_ton: float = 0.0
    invested_ton: float = 0.0

    win_rate: float = 0.0
    avg_win_ton: float = 0.0
    avg_loss_ton: float = 0.0
    expectancy_ton: float = 0.0

    median_hold_hours: float | None = None
    #: Насколько прогноз TTS расходится с фактом. >1 означает, что модель
    #: обещает продажу быстрее, чем получается.
    tts_bias: float | None = None

    max_drawdown_ton: float = 0.0
    best_day_ton: float = 0.0
    worst_day_ton: float = 0.0
    profitable_days: int = 0
    losing_days: int = 0

    daily: list[DayStat] = field(default_factory=list)
    by_collection: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "paper": self.paper,
            "closed_trades": self.closed_trades,
            "open_positions": self.open_positions,
            "realized_pnl_ton": round(self.realized_pnl_ton, 3),
            "unrealized_pnl_ton": round(self.unrealized_pnl_ton, 3),
            "invested_ton": round(self.invested_ton, 3),
            "win_rate": round(self.win_rate, 4),
            "avg_win_ton": round(self.avg_win_ton, 3),
            "avg_loss_ton": round(self.avg_loss_ton, 3),
            "expectancy_ton": round(self.expectancy_ton, 4),
            "median_hold_hours": (
                round(self.median_hold_hours, 1) if self.median_hold_hours else None
            ),
            "tts_bias": round(self.tts_bias, 2) if self.tts_bias else None,
            "max_drawdown_ton": round(self.max_drawdown_ton, 3),
            "best_day_ton": round(self.best_day_ton, 3),
            "worst_day_ton": round(self.worst_day_ton, 3),
            "profitable_days": self.profitable_days,
            "losing_days": self.losing_days,
            "daily": [
                {
                    "day": item.day,
                    "realized_ton": round(item.realized_ton, 3),
                    "trades": item.trades,
                }
                for item in self.daily
            ],
            "by_collection": {
                name: round(value, 3)
                for name, value in sorted(
                    self.by_collection.items(), key=lambda kv: kv[1], reverse=True
                )[:15]
            },
        }


async def compute(*, paper: bool, days: int = 30) -> TradingStats:
    since = utcnow() - timedelta(days=days)
    stats = TradingStats(paper=paper)

    async with session_scope() as session:
        closed = list(
            (
                await session.execute(
                    select(Position).where(
                        Position.paper.is_(paper),
                        Position.status == "closed",
                        Position.sold_at >= since,
                    )
                )
            ).scalars()
        )
        open_positions = list(
            (
                await session.execute(
                    select(Position).where(
                        Position.paper.is_(paper), Position.status.in_(("open", "listed"))
                    )
                )
            ).scalars()
        )

    stats.closed_trades = len(closed)
    stats.open_positions = len(open_positions)
    stats.invested_ton = sum(item.buy_price_ton for item in open_positions)

    # Бумажная переоценка считается по выставленной цене за вычетом
    # комиссии — не по флору. Флор это чужой аск, а не наша выручка.
    stats.unrealized_pnl_ton = sum(
        (item.ask_price_ton or item.buy_price_ton) * 0.95 - item.buy_price_ton
        for item in open_positions
    )

    if not closed:
        return stats

    pnls = [item.net_pnl_ton or 0.0 for item in closed]
    stats.realized_pnl_ton = sum(pnls)

    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    stats.win_rate = len(wins) / len(pnls)
    stats.avg_win_ton = statistics.mean(wins) if wins else 0.0
    stats.avg_loss_ton = statistics.mean(losses) if losses else 0.0
    stats.expectancy_ton = statistics.mean(pnls)

    holds = [
        (_aware(item.sold_at) - _aware(item.bought_at)).total_seconds() / 3600
        for item in closed
        if item.sold_at and item.bought_at
    ]
    if holds:
        stats.median_hold_hours = statistics.median(holds)

    predicted = [
        (item.expected_tts_hours, hold)
        for item, hold in zip(closed, holds, strict=False)
        if item.expected_tts_hours
    ]
    if predicted:
        ratios = [actual / expected for expected, actual in predicted if expected > 0]
        if ratios:
            stats.tts_bias = statistics.median(ratios)

    stats.daily = _daily_series(closed)
    if stats.daily:
        values = [item.realized_ton for item in stats.daily]
        stats.best_day_ton = max(values)
        stats.worst_day_ton = min(values)
        stats.profitable_days = sum(1 for value in values if value > 0)
        stats.losing_days = sum(1 for value in values if value < 0)
        stats.max_drawdown_ton = _max_drawdown(values)

    by_collection: dict[str, float] = defaultdict(float)
    for item in closed:
        by_collection[item.collection] += item.net_pnl_ton or 0.0
    stats.by_collection = dict(by_collection)

    return stats


def _daily_series(positions: list[Position]) -> list[DayStat]:
    buckets: dict[date, list[float]] = defaultdict(list)
    for item in positions:
        if item.sold_at is None:
            continue
        buckets[_aware(item.sold_at).date()].append(item.net_pnl_ton or 0.0)

    return [
        DayStat(day=day.isoformat(), realized_ton=sum(values), trades=len(values))
        for day, values in sorted(buckets.items())
    ]


def _max_drawdown(daily_pnl: list[float]) -> float:
    """Максимальная просадка кривой накопленной прибыли."""
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for value in daily_pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)
