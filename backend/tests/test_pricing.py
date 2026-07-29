"""Проверки модели справедливой цены."""

from __future__ import annotations

import math

import pytest
from app.analytics import pricing
from app.analytics.pricing import PricingContext
from app.domain import Attribute, AttributeKind, Gift

from tests.factories import TRUE_BASE, TRUE_MODEL_COEF, sales_population


def gift(model: str = "Epic", rarity: float = 10.0) -> Gift:
    return Gift(
        collection="Plush Pepe",
        external_id="x1",
        model=Attribute(kind=AttributeKind.MODEL, name=model, rarity_permille=rarity),
    )


def test_breakeven_markup_accounts_for_fee():
    # При комиссии 5% нужно 5.26% наценки, чтобы выйти в ноль.
    assert pricing.breakeven_markup(0.05) == pytest.approx(0.0526, abs=1e-3)
    assert pricing.breakeven_markup(0.0) == pytest.approx(0.0)


def test_net_roi_is_zero_at_breakeven():
    ask = 100.0
    exit_price = ask * (1 + pricing.breakeven_markup(0.05))
    roi = pricing.net_roi(ask, exit_price, fee_sell=0.05, gas_ton=0.0)
    assert roi == pytest.approx(0.0, abs=1e-9)


def test_net_roi_penalises_gas():
    with_gas = pricing.net_roi(10.0, 13.0, fee_sell=0.05, gas_ton=0.1)
    without_gas = pricing.net_roi(10.0, 13.0, fee_sell=0.05, gas_ton=0.0)
    assert with_gas < without_gas


def test_baseline_uses_attribute_floors():
    context = PricingContext(
        collection="Plush Pepe",
        floor_ton=10.0,
        attribute_floors={("model", "Epic"): 30.0},
    )
    value, components = pricing.baseline_value(context, gift())

    assert components["k_model"] == pytest.approx(3.0)
    # Модель втрое дороже флора — оценка должна быть заметно выше флора.
    assert value > 25.0


def test_baseline_dampens_secondary_attributes():
    """Три редких атрибута не должны перемножаться в лоб.

    Иначе редкая модель с редким фоном оценивалась бы кратно выше рынка,
    потому что редкости коррелированы.
    """
    context = PricingContext(
        collection="C",
        floor_ton=10.0,
        attribute_floors={
            ("model", "M"): 40.0,
            ("backdrop", "B"): 40.0,
            ("symbol", "S"): 40.0,
        },
    )
    full = Gift(
        collection="C",
        external_id="g",
        model=Attribute(kind=AttributeKind.MODEL, name="M"),
        backdrop=Attribute(kind=AttributeKind.BACKDROP, name="B"),
        symbol=Attribute(kind=AttributeKind.SYMBOL, name="S"),
    )
    value, components = pricing.baseline_value(context, full)

    naive = 10.0 * 4 * 4 * 4  # 640 — прямое перемножение
    assert value < naive / 2
    assert components["k_combined"] < 4 * 4 * 4


def test_regression_recovers_known_model():
    """Регрессия должна восстановить формулу, по которой сгенерированы данные."""
    context = PricingContext(
        collection="Plush Pepe", floor_ton=TRUE_BASE, sales=sales_population(80, noise=0.05)
    )
    fit = pricing.fit_regression(context)
    assert fit is not None

    # Коэффициент при редкости модели — второй в векторе признаков.
    assert fit.coefficients[1] == pytest.approx(TRUE_MODEL_COEF, abs=0.08)

    predicted, _ = pricing.regression_value(fit, gift("Epic", 10.0))
    expected = TRUE_BASE * math.exp(TRUE_MODEL_COEF * -math.log(10.0 / 1000.0))
    assert predicted == pytest.approx(expected, rel=0.15)


def test_regression_needs_minimum_sample():
    context = PricingContext(collection="C", floor_ton=10.0, sales=sales_population(5))
    assert pricing.fit_regression(context) is None


def test_estimate_falls_back_to_baseline_without_history():
    context = PricingContext(
        collection="C", floor_ton=10.0, attribute_floors={("model", "Epic"): 20.0}
    )
    result = pricing.estimate(context, gift())

    assert result.method == "baseline"
    assert result.is_usable
    # Без истории доверие должно быть ограниченным.
    assert result.confidence <= 0.35


def test_estimate_confidence_grows_with_data():
    floors = {("model", "Epic"): 20.0}
    thin = PricingContext(
        collection="C", floor_ton=10.0, attribute_floors=floors, sales=sales_population(12)
    )
    rich = PricingContext(
        collection="C", floor_ton=10.0, attribute_floors=floors, sales=sales_population(120)
    )

    assert pricing.estimate(rich, gift()).confidence > pricing.estimate(thin, gift()).confidence


def test_conservative_never_exceeds_fair():
    context = PricingContext(
        collection="C",
        floor_ton=10.0,
        attribute_floors={("model", "Epic"): 25.0},
        sales=sales_population(90),
    )
    result = pricing.estimate(context, gift())
    assert result.conservative_ton <= result.value_ton


def test_wild_regression_is_damped():
    """Если история расходится с флором в разы, вес регрессии снижается."""
    crazy = sales_population(60)
    for sale in crazy:
        sale.price_ton *= 50  # история «с другой планеты»

    context = PricingContext(
        collection="C",
        floor_ton=10.0,
        attribute_floors={("model", "Epic"): 20.0},
        sales=crazy,
    )
    result = pricing.estimate(context, gift())
    baseline, _ = pricing.baseline_value(context, gift())

    # Оценка не должна улететь вслед за аномальной историей.
    assert result.value_ton < baseline * 20
