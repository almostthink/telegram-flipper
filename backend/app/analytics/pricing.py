"""Оценка справедливой цены подарка.

Две модели, которые дополняют друг друга:

**Baseline — мультипликативная.** ``floor × k_model × k_backdrop × k_symbol``,
где множители берутся из флоров атрибутов, отдаваемых площадкой. Работает
сразу, даже без истории, но грубая: флор атрибута — это самое дешёвое
предложение, а не цена сделки.

**Регрессия по фактическим продажам.** ``log(price) ~ редкость + тренд``,
взвешенная по свежести. Точнее, но требует выборки.

Между ними плавный переход по объёму данных: пока сделок мало, вес у
регрессии низкий. Резкого переключения нет — иначе оценка скакала бы на
каждой новой продаже.

Почему логарифм цены: цены подарков распределены логнормально (разброс
кратный, а не аддитивный), и в логарифмах регрессия становится линейной,
а выбросы перестают тянуть коэффициенты.

Почему не берём медиану листингов: листинги показывают, чего хотят
продавцы. Платит рынок другое.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from app.analytics import color as color_mod
from app.analytics import numerology
from app.analytics.rarity import known_rarity_count, rarity_score, rarity_scores
from app.domain import AttributeKind, Gift, utcnow
from app.storage.models import SaleRecord

log = logging.getLogger(__name__)

#: Множитель атрибута ограничиваем: флор редкого атрибута иногда задран
#: единственным неадекватным листингом, и без клипа оценка улетает.
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 25.0

#: Квантиль остатков для консервативной цены выхода.
CONSERVATIVE_QUANTILE = 0.25

#: Во сколько раз регрессия имеет право разойтись с baseline, прежде чем
#: её результат будет принудительно зажат в коридор.
SANE_RATIO_LOW = 0.2
SANE_RATIO_HIGH = 5.0


@dataclass(slots=True)
class FairValue:
    """Результат оценки."""

    value_ton: float
    #: Цена, по которой продать реалистично быстро (нижний квантиль).
    conservative_ton: float
    #: 0..1 — насколько модель себе доверяет.
    confidence: float
    method: str
    components: dict[str, float] = field(default_factory=dict)
    sample_size: int = 0

    @property
    def is_usable(self) -> bool:
        return self.value_ton > 0 and self.confidence > 0


@dataclass(slots=True)
class PricingContext:
    """Всё, что нужно для оценки одной коллекции."""

    collection: str
    floor_ton: float
    attribute_floors: dict[tuple[str, str], float] = field(default_factory=dict)
    sales: list[SaleRecord] = field(default_factory=list)
    min_samples: int = 30
    halflife_days: float = 7.0
    #: Максимальная надбавка к baseline за идеальный номер (#1).
    number_bonus: float = 0.35
    #: Названия фонов, которые рынок ценит отдельно от их редкости.
    preferred_backdrops: list[str] = field(default_factory=list)
    #: Надбавка за попадание фона в этот список.
    backdrop_bonus: float = 0.15
    #: Цвета моделей коллекции: имя модели → 0xRRGGBB. Пусто, пока цвета
    #: не добыты — тогда монохром просто не учитывается.
    model_colors: dict[str, int] = field(default_factory=dict)
    #: Надбавка за монохром: цвет модели совпал с цветом фона.
    monochrome_bonus: float = 0.20


# --- Baseline ------------------------------------------------------------


def attribute_multiplier(
    context: PricingContext, kind: AttributeKind, name: str | None
) -> float:
    """Во сколько раз атрибут дороже базового флора коллекции."""
    if not name or context.floor_ton <= 0:
        return 1.0
    floor = context.attribute_floors.get((kind.value, name))
    if not floor or floor <= 0:
        return 1.0
    return float(np.clip(floor / context.floor_ton, MIN_MULTIPLIER, MAX_MULTIPLIER))


def collectible_premium(
    context: PricingContext, gift: Gift
) -> tuple[float, dict[str, float]]:
    """Надбавка за коллекционные признаки: номер выпуска и фон.

    Нужна только для baseline. Как только накопится история сделок,
    регрессия оценит эти же факторы по реальным ценам, и надбавка отсюда
    получит соответственно меньший вес.

    Множители складываются, а не перемножаются: красивый номер и ценный
    фон усиливают друг друга, но не кратно.
    """
    trait = numerology.classify(gift.number)
    number_part = context.number_bonus * trait.score

    backdrop_part = 0.0
    if gift.backdrop is not None and context.preferred_backdrops:
        wanted = {name.strip().lower() for name in context.preferred_backdrops}
        if gift.backdrop.name.strip().lower() in wanted:
            backdrop_part = context.backdrop_bonus

    # Монохром — совпадение цвета модели с цветом фона. Оценивается
    # долей, а не признаком: близкий, но заметно другой оттенок стоит
    # между «в тон» и «мимо», и резкий порог здесь дал бы скачок цены.
    mono = monochrome_score(context, gift)
    mono_part = context.monochrome_bonus * (mono or 0.0)

    premium = 1.0 + number_part + backdrop_part + mono_part
    return premium, {
        "number_score": round(trait.score, 3),
        "premium_number": round(number_part, 3),
        "premium_backdrop": round(backdrop_part, 3),
        "monochrome_score": round(mono, 3) if mono is not None else -1.0,
        "premium_monochrome": round(mono_part, 3),
    }


def monochrome_score(context: PricingContext, gift: Gift) -> float | None:
    """Насколько цвет модели совпадает с цветом фона: 0..1 или None.

    None означает «неизвестно»: цвет модели ещё не добыт или площадка не
    прислала цвет фона. Это не ноль — отсутствие данных не должно
    работать как уверенное «не монохром».
    """
    if gift.model is None or gift.backdrop_color is None:
        return None
    model_rgb = context.model_colors.get(gift.model.name)
    if model_rgb is None:
        return None
    return color_mod.match_score(
        color_mod.from_int(model_rgb), color_mod.from_int(gift.backdrop_color)
    )


def baseline_value(context: PricingContext, gift: Gift) -> tuple[float, dict[str, float]]:
    multipliers = {
        "k_model": attribute_multiplier(
            context, AttributeKind.MODEL, gift.model.name if gift.model else None
        ),
        "k_backdrop": attribute_multiplier(
            context, AttributeKind.BACKDROP, gift.backdrop.name if gift.backdrop else None
        ),
        "k_symbol": attribute_multiplier(
            context, AttributeKind.SYMBOL, gift.symbol.name if gift.symbol else None
        ),
    }

    # Перемножать три множителя целиком нельзя: они не независимы, редкая
    # модель часто идёт с редким фоном, и произведение переоценивает лот.
    # Полный вес даём самому сильному, остальные учитываем ослабленно.
    ordered = sorted(multipliers.values(), reverse=True)
    combined = ordered[0] * (ordered[1] ** 0.5) * (ordered[2] ** 0.25)

    premium, premium_parts = collectible_premium(context, gift)

    value = context.floor_ton * combined * premium
    return value, {
        **multipliers,
        **premium_parts,
        "k_combined": combined,
        "collectible_premium": round(premium, 3),
        "floor": context.floor_ton,
    }


# --- Регрессия -----------------------------------------------------------


def _features(
    model_rarity: float,
    backdrop_rarity: float,
    symbol_rarity: float,
    number_score: float,
    days_ago: float,
) -> list[float]:
    """Вектор признаков.

    ``number_score`` — коллекционная ценность порядкового номера. Это
    отдельная ось: рынок платит за #1 или #7777 надбавку, никак не
    связанную с редкостью модели и фона. Коэффициент при нём модель
    выучивает по фактическим сделкам, а не берёт из предположений.

    Тренд по времени вбирает дрейф флора коллекции.
    """
    return [1.0, model_rarity, backdrop_rarity, symbol_rarity, number_score, days_ago]


def _sale_features(sale: SaleRecord, now: datetime) -> tuple[list[float], float] | None:
    if sale.price_ton is None or sale.price_ton <= 0:
        return None
    sold_at = sale.sold_at if sale.sold_at.tzinfo else sale.sold_at.replace(tzinfo=UTC)
    days_ago = max((now - sold_at).total_seconds() / 86400, 0.0)

    from app.domain import Attribute

    def score(name: str | None, rarity: float | None, kind: AttributeKind) -> float:
        if name is None:
            return rarity_score(None)
        return rarity_score(Attribute(kind=kind, name=name, rarity_permille=rarity))

    features = _features(
        score(sale.model, sale.model_rarity, AttributeKind.MODEL),
        score(sale.backdrop, sale.backdrop_rarity, AttributeKind.BACKDROP),
        score(sale.symbol, sale.symbol_rarity, AttributeKind.SYMBOL),
        numerology.score(sale.number),
        days_ago,
    )
    return features, math.log(sale.price_ton)


@dataclass(slots=True)
class RegressionFit:
    coefficients: np.ndarray
    residual_std: float
    #: Смещение медианы и нижнего квантиля остатков — переводит центр
    #: регрессии в реальные квантили цены.
    median_offset: float
    conservative_offset: float
    sample_size: int
    effective_n: float


def fit_regression(context: PricingContext, now: datetime | None = None) -> RegressionFit | None:
    """Взвешенный МНК по логарифму цены.

    Свежие сделки весят больше: рынок подарков меняется за недели, и сделка
    месячной давности почти не говорит о сегодняшней цене. Вес падает вдвое
    каждые ``halflife_days``.
    """
    now = now or utcnow()
    rows: list[list[float]] = []
    targets: list[float] = []
    weights: list[float] = []

    for sale in context.sales:
        if sale.suspicious:
            continue
        prepared = _sale_features(sale, now)
        if prepared is None:
            continue
        features, log_price = prepared
        days_ago = features[-1]
        rows.append(features)
        targets.append(log_price)
        weights.append(0.5 ** (days_ago / context.halflife_days))

    if not rows or len(rows) < len(rows[0]) * 2:
        # Точек должно быть заметно больше, чем коэффициентов, иначе
        # регрессия просто запомнит выборку вместо того, чтобы обобщить.
        return None

    X = np.asarray(rows, dtype=float)
    y = np.asarray(targets, dtype=float)
    w = np.asarray(weights, dtype=float)

    # Винзоризация: обрезаем 2% хвостов, чтобы одна аномальная сделка
    # не перекосила все коэффициенты.
    low, high = np.quantile(y, [0.02, 0.98])
    y = np.clip(y, low, high)

    sqrt_w = np.sqrt(w)
    Xw = X * sqrt_w[:, None]
    yw = y * sqrt_w

    try:
        coefficients, *_ = np.linalg.lstsq(Xw, yw, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coefficients)):
        return None

    residuals = y - X @ coefficients
    # Квантили остатков считаем взвешенно — по тем же свежим сделкам.
    median_offset = float(_weighted_quantile(residuals, w, 0.5))
    conservative_offset = float(_weighted_quantile(residuals, w, CONSERVATIVE_QUANTILE))
    residual_std = float(np.sqrt(np.average((residuals - median_offset) ** 2, weights=w)))

    return RegressionFit(
        coefficients=coefficients,
        residual_std=residual_std,
        median_offset=median_offset,
        conservative_offset=conservative_offset,
        sample_size=len(rows),
        effective_n=float(w.sum()),
    )


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    sorted_values = values[order]
    sorted_weights = weights[order]
    cumulative = np.cumsum(sorted_weights)
    if cumulative[-1] <= 0:
        return float(np.quantile(values, q))
    cutoff = q * cumulative[-1]
    index = int(np.searchsorted(cumulative, cutoff))
    return float(sorted_values[min(index, len(sorted_values) - 1)])


def regression_value(fit: RegressionFit, gift: Gift) -> tuple[float, float]:
    """Предсказание на сегодня (days_ago = 0): медиана и нижний квантиль."""
    model_r, backdrop_r, symbol_r = rarity_scores(gift)
    features = np.asarray(
        _features(model_r, backdrop_r, symbol_r, numerology.score(gift.number), 0.0)
    )
    center = float(features @ fit.coefficients)
    return (
        math.exp(center + fit.median_offset),
        math.exp(center + fit.conservative_offset),
    )


# --- Итоговая оценка -----------------------------------------------------


def estimate(context: PricingContext, gift: Gift, now: datetime | None = None) -> FairValue:
    """Справедливая цена: смесь baseline и регрессии по объёму данных."""
    base_value, components = baseline_value(context, gift)
    if base_value <= 0:
        return FairValue(0.0, 0.0, 0.0, "нет данных", components)

    fit = fit_regression(context, now)
    if fit is None:
        # Только baseline: доверие ограничено сверху, потому что флоры
        # атрибутов систематически ниже цен реальных сделок.
        confidence = 0.35 * _rarity_completeness(gift)
        return FairValue(
            value_ton=base_value,
            conservative_ton=base_value * 0.9,
            confidence=round(confidence, 3),
            method="baseline",
            components=components,
            sample_size=len(context.sales),
        )

    reg_value, reg_conservative = regression_value(fit, gift)

    # Вес регрессии растёт с эффективным числом наблюдений и достигает
    # единицы на min_samples. Переход плавный, без скачка оценки.
    weight = float(np.clip(fit.effective_n / max(context.min_samples, 1), 0.0, 1.0))

    # Санитарная проверка: если регрессия расходится с baseline в разы,
    # доверять ей нельзя — скорее всего выборка нерепрезентативна или
    # засорена. Понижения веса тут мало: при расхождении в 200 раз даже
    # вес 0.3 уводит итог на порядок. Поэтому саму величину зажимаем в
    # коридор вокруг baseline и только потом смешиваем.
    ratio = reg_value / base_value if base_value > 0 else 0.0
    if not SANE_RATIO_LOW <= ratio <= SANE_RATIO_HIGH:
        log.debug(
            "%s: регрессия разошлась с baseline в %.1f раза — зажимаю в коридор",
            context.collection, ratio,
        )
        clamped = float(
            np.clip(reg_value, base_value * SANE_RATIO_LOW, base_value * SANE_RATIO_HIGH)
        )
        reg_conservative *= clamped / reg_value if reg_value > 0 else 1.0
        reg_value = clamped
        weight *= 0.3

    value = weight * reg_value + (1 - weight) * base_value
    conservative = weight * reg_conservative + (1 - weight) * base_value * 0.9

    # Доверие падает с ростом разброса остатков: широкий разброс означает,
    # что редкость объясняет цену плохо и точечная оценка ненадёжна.
    dispersion_penalty = 1.0 / (1.0 + fit.residual_std)
    confidence = float(
        np.clip(
            (0.35 + 0.65 * weight) * dispersion_penalty * _rarity_completeness(gift),
            0.0,
            0.95,
        )
    )

    components.update(
        {
            "baseline": base_value,
            "regression": reg_value,
            "regression_weight": weight,
            "residual_std": fit.residual_std,
        }
    )

    return FairValue(
        value_ton=value,
        conservative_ton=min(conservative, value),
        confidence=round(confidence, 3),
        method="регрессия+baseline" if weight > 0.15 else "baseline",
        components=components,
        sample_size=fit.sample_size,
    )


def _rarity_completeness(gift: Gift) -> float:
    """Штраф за неизвестные редкости: оценка вслепую заслуживает меньше веры."""
    return 0.6 + 0.4 * (known_rarity_count(gift) / 3.0)


# --- Экономика сделки ----------------------------------------------------


def net_roi(
    ask_ton: float,
    exit_ton: float,
    *,
    fee_sell: float,
    fee_buy: float = 0.0,
    gas_ton: float = 0.0,
) -> float:
    """Чистая доходность сделки после всех издержек.

    Продавец платит комиссию площадки, поэтому на руки приходит
    ``exit × (1 − fee_sell)``. Газ считаем дважды: покупка и продажа.
    """
    cost = ask_ton * (1 + fee_buy) + gas_ton
    proceeds = exit_ton * (1 - fee_sell) - gas_ton
    if cost <= 0:
        return 0.0
    return (proceeds - cost) / cost


def breakeven_markup(fee_sell: float, fee_buy: float = 0.0) -> float:
    """Наценка, при которой сделка выходит в ноль.

    При комиссии 5% это 5.3%: спред «8%» на деле даёт 2.6%, а не 8%.
    """
    if fee_sell >= 1:
        return math.inf
    return (1 + fee_buy) / (1 - fee_sell) - 1
