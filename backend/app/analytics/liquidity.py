"""Оценка ликвидности коллекции.

Главный фильтр всей системы. Скидка 40% на подарке, который никто не
покупает, — это не прибыль, а замороженные деньги. Поэтому ликвидность
входит в итоговый балл сигнала множителем, а не слагаемым: при нулевой
ликвидности любой ROI даёт ноль.

Нормализация везде насыщающая: ``x / (x + k)``. Это избавляет от
подгонки шкал под конкретный рынок и держит все компоненты в 0..1.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import timedelta

from app.domain import utcnow
from app.storage.models import ListingSnapshot, SaleRecord

#: Точки половинного насыщения — при таком значении компонент даёт 0.5.
SALES_PER_DAY_HALF = 2.0
TTS_HALF_HOURS = 24.0
DEPTH_HALF_COUNT = 20.0

WEIGHTS = {
    "velocity": 0.35,
    "tts": 0.30,
    "bid_support": 0.20,
    "depth": 0.15,
}


@dataclass(slots=True)
class LiquidityMetrics:
    collection: str
    sales_24h: int = 0
    sales_7d: int = 0
    tts_median_hours: float | None = None
    #: Сколько лотов стоит в пределах +10% от флора. Толстая книга давит цену.
    depth_10pct: int = 0
    #: Лучший коллекционный оффер как доля от флора — цена гарантированного выхода.
    bid_support: float = 0.0
    spread: float | None = None
    floor_trend_24h: float | None = None
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)

    @property
    def expected_tts_hours(self) -> float:
        """Прогноз времени до продажи.

        Если фактических замеров нет, оцениваем через скорость продаж и
        глубину книги: чтобы продать, нужно дождаться, пока разберут всё,
        что стоит дешевле.
        """
        if self.tts_median_hours is not None:
            return self.tts_median_hours
        per_day = self.sales_7d / 7 if self.sales_7d else 0.0
        if per_day <= 0:
            return 24 * 14  # две недели — фактически «не продаётся»
        queue = max(self.depth_10pct, 1)
        return min(queue / per_day * 24, 24 * 14)


def compute(
    collection: str,
    *,
    sales: list[SaleRecord],
    listings: list[ListingSnapshot],
    floor_ton: float | None,
    best_offer_ton: float | None = None,
    floor_24h_ago: float | None = None,
) -> LiquidityMetrics:
    now = utcnow()
    metrics = LiquidityMetrics(collection=collection)

    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    clean = [sale for sale in sales if not sale.suspicious]
    metrics.sales_24h = sum(1 for sale in clean if _aware(sale.sold_at) >= day_ago)
    metrics.sales_7d = sum(1 for sale in clean if _aware(sale.sold_at) >= week_ago)

    measured = [sale.tts_hours for sale in clean if sale.tts_hours is not None]
    if measured:
        metrics.tts_median_hours = float(statistics.median(measured))

    if floor_ton and floor_ton > 0:
        metrics.depth_10pct = sum(
            1 for listing in listings if listing.price_ton <= floor_ton * 1.10
        )
        if best_offer_ton and best_offer_ton > 0:
            metrics.bid_support = best_offer_ton / floor_ton
            metrics.spread = 1 - metrics.bid_support
        if floor_24h_ago and floor_24h_ago > 0:
            metrics.floor_trend_24h = floor_ton / floor_24h_ago - 1

    metrics.parts = _components(metrics)
    metrics.score = round(
        sum(WEIGHTS[name] * value for name, value in metrics.parts.items()), 4
    )
    return metrics


def _components(metrics: LiquidityMetrics) -> dict[str, float]:
    per_day = metrics.sales_7d / 7 if metrics.sales_7d else 0.0
    velocity = per_day / (per_day + SALES_PER_DAY_HALF)

    tts = metrics.expected_tts_hours
    tts_component = TTS_HALF_HOURS / (TTS_HALF_HOURS + tts)

    # Оффер ниже половины флора выходом считать нельзя — это не поддержка,
    # а попытка выкупить дёшево.
    bid = 0.0
    if metrics.bid_support >= 0.5:
        bid = min((metrics.bid_support - 0.5) / 0.45, 1.0)

    # Глубина — штраф: чем больше конкурирующих лотов у флора, тем дольше
    # очередь на продажу. Компонент инвертирован, поэтому меньше — хуже.
    depth_penalty = metrics.depth_10pct / (metrics.depth_10pct + DEPTH_HALF_COUNT)
    depth = 1.0 - depth_penalty

    return {
        "velocity": round(velocity, 4),
        "tts": round(tts_component, 4),
        "bid_support": round(bid, 4),
        "depth": round(depth, 4),
    }


def _aware(value):
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)
