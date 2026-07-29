"""Генерация торговых сигналов.

Собирает вместе справедливую цену, ликвидность и антифрод и решает,
стоит ли покупать лот.

Порядок важен: сначала дешёвые жёсткие отсечки, потом дорогой расчёт.
Считать регрессию по коллекции, которая всё равно не пройдёт по
ликвидности, — пустая трата времени.

Экономика считается по **консервативной** цене выхода, а не по
справедливой. Справедливая — это где-то посередине книги, а выйти нужно
быстро, значит по нижнему квантилю. Иначе прогноз ROI систематически
завышен, и на бумаге стратегия прибыльна, а на счёте нет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC
from enum import StrEnum

from app.analytics import antifraud, numerology, pricing
from app.analytics import liquidity as liquidity_mod
from app.analytics.pricing import FairValue, PricingContext
from app.config import AnalyticsConfig, CollectibleConfig
from app.domain import Attribute, AttributeKind, Gift, Market, utcnow
from app.storage.models import ListingSnapshot, SignalRecord

log = logging.getLogger(__name__)


class Reject(StrEnum):
    """Причины отказа. Сохраняются в журнал: по ним видно, не слишком ли
    строгие пороги и что именно отсекает большинство лотов."""

    LOW_ROI = "низкий ROI"
    LOW_LIQUIDITY = "низкая ликвидность"
    SLOW_TTS = "долгая продажа"
    FALLING_FLOOR = "падающий флор"
    SUSPICIOUS = "подозрительный лот"
    NO_FAIR_VALUE = "нет оценки цены"
    LOW_CONFIDENCE = "низкое доверие к оценке"
    CROSS_MARKET = "дёшево на всех площадках"
    TOO_EXPENSIVE = "выше лимита на позицию"
    RESALE_LOCKED = "перепродажа заблокирована"
    NOT_COLLECTIBLE = "нет коллекционных признаков"


@dataclass(slots=True)
class Signal:
    listing: ListingSnapshot
    fair: FairValue
    liquidity: liquidity_mod.LiquidityMetrics
    net_roi: float
    score: float
    passed: bool
    reject_reason: Reject | None = None
    explanation: str = ""

    @property
    def market(self) -> Market:
        return Market(self.listing.market)

    def to_record(self) -> SignalRecord:
        trait = numerology.classify(self.listing.number)
        return SignalRecord(
            market=self.listing.market,
            listing_id=self.listing.listing_id,
            collection=self.listing.collection,
            number=self.listing.number,
            number_score=trait.score,
            number_label=trait.label if trait.is_notable else None,
            model=self.listing.model,
            backdrop=self.listing.backdrop,
            symbol=self.listing.symbol,
            ask_ton=self.listing.price_ton,
            fair_value_ton=self.fair.value_ton,
            net_roi=self.net_roi,
            liquidity_score=self.liquidity.score,
            confidence=self.fair.confidence,
            expected_tts_hours=self.liquidity.expected_tts_hours,
            score=self.score,
            passed=self.passed,
            reject_reason=self.reject_reason.value if self.reject_reason else None,
            explanation=self.explanation,
        )


@dataclass(slots=True)
class EvaluationInput:
    """Всё, что нужно для оценки одной коллекции за один проход."""

    collection: str
    listings: list[ListingSnapshot]
    context: PricingContext
    liquidity: liquidity_mod.LiquidityMetrics
    fee_sell_by_market: dict[str, float] = field(default_factory=dict)
    fee_buy_by_market: dict[str, float] = field(default_factory=dict)
    #: Минимальный флор по остальным площадкам — для кросс-маркет сверки.
    cross_market_floor: float | None = None
    max_position_ton: float | None = None


def listing_to_gift(listing: ListingSnapshot) -> Gift:
    def attribute(name: str | None, rarity: float | None, kind: AttributeKind):
        if not name:
            return None
        return Attribute(kind=kind, name=name, rarity_permille=rarity)

    return Gift(
        collection=listing.collection,
        external_id=listing.listing_id,
        model=attribute(listing.model, listing.model_rarity, AttributeKind.MODEL),
        backdrop=attribute(listing.backdrop, listing.backdrop_rarity, AttributeKind.BACKDROP),
        symbol=attribute(listing.symbol, listing.symbol_rarity, AttributeKind.SYMBOL),
    )


def is_collectible(listing: ListingSnapshot, config: CollectibleConfig) -> bool:
    """Есть ли у лота признаки, за которые рынок платит отдельно.

    Два независимых основания: заметный порядковый номер либо фон из
    списка предпочитаемых.
    """
    if numerology.score(listing.number) >= config.min_number_score:
        return True
    if listing.backdrop and config.preferred_backdrops:
        wanted = {name.strip().lower() for name in config.preferred_backdrops}
        if listing.backdrop.strip().lower() in wanted:
            return True
    return False


def evaluate_collection(
    data: EvaluationInput,
    config: AnalyticsConfig,
    collectible: CollectibleConfig | None = None,
) -> list[Signal]:
    """Оцениваем все листинги коллекции."""
    collectible = collectible or CollectibleConfig()
    signals: list[Signal] = []

    # Коллекционные отсечки: если не проходит коллекция, не проходит ни один
    # её лот. Считаем один раз, а не для каждого листинга.
    collection_reject = _collection_level_reject(data, config)

    for listing in data.listings:
        signal = _evaluate_listing(listing, data, config, collectible, collection_reject)
        signals.append(signal)

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals


def _collection_level_reject(data: EvaluationInput, config: AnalyticsConfig) -> Reject | None:
    if data.liquidity.score < config.min_liquidity_score:
        return Reject.LOW_LIQUIDITY
    if data.liquidity.expected_tts_hours > config.max_tts_hours:
        return Reject.SLOW_TTS

    trend = data.liquidity.floor_trend_24h
    if trend is not None and trend < -config.max_floor_drop_24h:
        # Ловить падающий нож — самый быстрый способ потерять депозит.
        return Reject.FALLING_FLOOR
    return None


def _evaluate_listing(
    listing: ListingSnapshot,
    data: EvaluationInput,
    config: AnalyticsConfig,
    collectible: CollectibleConfig,
    collection_reject: Reject | None,
) -> Signal:
    gift = listing_to_gift(listing)
    fair = pricing.estimate(data.context, gift)

    fee_sell = data.fee_sell_by_market.get(listing.market, 0.05)
    fee_buy = data.fee_buy_by_market.get(listing.market, 0.0)

    roi = pricing.net_roi(
        listing.price_ton,
        fair.conservative_ton,
        fee_sell=fee_sell,
        fee_buy=fee_buy,
        gas_ton=config.gas_ton,
    )

    score = max(roi, 0.0) * data.liquidity.score * fair.confidence
    reject = collection_reject or _listing_level_reject(
        listing, data, config, collectible, fair, roi
    )

    return Signal(
        listing=listing,
        fair=fair,
        liquidity=data.liquidity,
        net_roi=roi,
        score=round(score, 5),
        passed=reject is None,
        reject_reason=reject,
        explanation=_explain(listing, fair, data.liquidity, roi, fee_sell, reject),
    )


def _listing_level_reject(
    listing: ListingSnapshot,
    data: EvaluationInput,
    config: AnalyticsConfig,
    collectible: CollectibleConfig,
    fair: FairValue,
    roi: float,
) -> Reject | None:
    if not fair.is_usable:
        return Reject.NO_FAIR_VALUE
    if fair.confidence < 0.25:
        return Reject.LOW_CONFIDENCE
    if data.max_position_ton is not None and listing.price_ton > data.max_position_ton:
        return Reject.TOO_EXPENSIVE
    if roi < config.min_roi:
        return Reject.LOW_ROI

    # Блокировка перепродажи — отказ без обсуждения. Купить подарок,
    # который нельзя продать ещё несколько дней, значит заморозить
    # капитал: единственный рабочий ресурс флиппера.
    if listing.locked:
        return Reject.RESALE_LOCKED
    if listing.resale_available_at is not None:
        unlock = listing.resale_available_at
        if unlock.tzinfo is None:
            unlock = unlock.replace(tzinfo=UTC)
        if unlock > utcnow():
            return Reject.RESALE_LOCKED

    # Режим «только коллекционное»: покупаем лишь то, за что рынок платит
    # надбавку сверх атрибутов. Заметно сужает выдачу, поэтому выключен
    # по умолчанию.
    if collectible.require_collectible and not is_collectible(listing, collectible):
        return Reject.NOT_COLLECTIBLE

    if antifraud.is_suspicious_listing(listing, data.listings):
        return Reject.SUSPICIOUS

    # Кросс-маркет сверка: лот дёшев относительно нашей оценки, но если он
    # так же дёшев на других площадках — упала не цена лота, а весь рынок.
    if (
        data.cross_market_floor is not None
        and data.cross_market_floor > 0
        and listing.price_ton >= data.cross_market_floor * (1 - config.min_roi / 2)
    ):
        return Reject.CROSS_MARKET

    return None


def _explain(
    listing: ListingSnapshot,
    fair: FairValue,
    metrics: liquidity_mod.LiquidityMetrics,
    roi: float,
    fee_sell: float,
    reject: Reject | None,
) -> str:
    """Человекочитаемое обоснование — попадает в журнал и в интерфейс.

    Без него невозможно понять постфактум, почему бот купил именно это,
    и что именно надо чинить, когда сделка окажется убыточной.
    """
    breakeven = pricing.breakeven_markup(fee_sell)
    head = (
        f"Аск {listing.price_ton:.2f} TON, справедливая {fair.value_ton:.2f}, "
        f"выход по {fair.conservative_ton:.2f} → чистый ROI {roi * 100:.1f}% "
        f"(безубыток при наценке {breakeven * 100:.1f}%)."
    )
    body = (
        f" Метод: {fair.method}, доверие {fair.confidence:.2f}, "
        f"выборка {fair.sample_size} сделок. "
        f"Ликвидность {metrics.score:.2f} "
        f"(продаж за 7д: {metrics.sales_7d}, ожидаемая продажа "
        f"~{metrics.expected_tts_hours:.0f}ч, у флора {metrics.depth_10pct} лотов)."
    )
    trait = numerology.classify(listing.number)
    collectible_note = ""
    if listing.number:
        collectible_note = f" Номер #{listing.number}"
        if trait.is_notable:
            collectible_note += f" — {trait.label}, рынок платит за такие надбавку."
        else:
            collectible_note += "."

    tail = f" ОТКЛОНЁН: {reject.value}." if reject else " Прошёл все фильтры."
    return head + body + collectible_note + tail
