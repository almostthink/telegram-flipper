"""Проверки ордер-движка.

Движок встаёт в очередь покупателей чуть ниже флора и перебивает
конкурентов, не выходя за спред. Прибыль здесь берётся из разницы «купил
ниже флора — продал у флора», поэтому потолок цены и есть вся стратегия:
стоит его нарушить, и сделка перестаёт окупать комиссию.
"""

from __future__ import annotations

import pytest
from app.trading.orders import (
    CollectionBook,
    OrderAction,
    OrderPolicy,
    ceiling_price,
    plan_orders,
)


def policy(**overrides) -> OrderPolicy:
    base = {
        "target_spread": 0.10,
        "min_floor_ton": 0.0,
        "max_floor_ton": 100.0,
        "amount": 1,
        "max_orders": 20,
        "tick_ton": 0.01,
        "fee_sell": 0.02,
    }
    base.update(overrides)
    return OrderPolicy(**base)


def book(**overrides) -> CollectionBook:
    base = {"collection": "Evil Eye", "floor_ton": 10.0}
    base.update(overrides)
    return CollectionBook(**base)


def only(plan):
    assert len(plan.intents) == 1, plan.intents
    return plan.intents[0]


# --- Потолок цены ---------------------------------------------------------


def test_ceiling_is_floor_minus_spread():
    assert ceiling_price(10.0, policy(target_spread=0.10)) == pytest.approx(9.0)


def test_empty_queue_is_taken_at_the_ceiling():
    """Спорить не с кем, но выше потолка не поднимаемся никогда."""
    intent = only(plan_orders([book()], policy()))

    assert intent.action is OrderAction.PLACE
    assert intent.price_ton == pytest.approx(9.0)


def test_competitor_is_outbid_by_one_tick():
    """Дать больше нужного — подарить разницу продавцу."""
    intent = only(plan_orders([book(top_other_ton=8.0)], policy()))

    assert intent.price_ton == pytest.approx(8.01)
    assert "перебиваем" in intent.reason


def test_competitor_above_the_ceiling_is_left_alone():
    """Он торгует без запаса — ввязываться в это не наша задача."""
    plan = plan_orders([book(top_other_ton=9.5)], policy())

    assert plan.intents == []
    assert "потолк" in plan.skipped["Evil Eye"]


def test_outbidding_never_crosses_the_ceiling():
    """Конкурент ровно у потолка: шаг перебивания вывел бы нас за него."""
    plan = plan_orders([book(top_other_ton=9.0)], policy())

    assert plan.intents == []


# --- Границы флора --------------------------------------------------------


def test_expensive_collections_are_skipped():
    plan = plan_orders([book(floor_ton=50.0)], policy(max_floor_ton=4.0))

    assert plan.intents == []
    assert "выше верхней" in plan.skipped["Evil Eye"]


def test_cheap_collections_are_skipped():
    plan = plan_orders([book(floor_ton=0.2)], policy(min_floor_ton=1.0))

    assert "ниже нижней" in plan.skipped["Evil Eye"]


def test_collection_without_floor_is_skipped():
    assert plan_orders([book(floor_ton=0.0)], policy()).intents == []


# --- Экономика ------------------------------------------------------------


def test_spread_below_the_fee_stops_everything():
    """Купить со скидкой меньше комиссии — заплатить площадке за свой труд.

    Отказ общий, а не по коллекциям: дело не в рынке, а в настройке.
    """
    plan = plan_orders([book(), book(collection="Другая")], policy(target_spread=0.01))

    assert plan.intents == []
    assert "комиссию" in plan.skipped["*"]


def test_spread_above_the_fee_works():
    assert plan_orders([book()], policy(target_spread=0.03, fee_sell=0.02)).intents


# --- Уже стоящие заявки ---------------------------------------------------


def test_our_order_is_raised_when_outbid():
    plan = plan_orders(
        [book(top_other_ton=8.5, my_order_ton=8.0, my_order_id="O1")], policy()
    )
    intent = only(plan)

    assert intent.action is OrderAction.RAISE
    assert intent.order_id == "O1"
    assert intent.price_ton == pytest.approx(8.51)


def test_leading_order_is_left_alone():
    """Мы уже первые — трогать заявку незачем."""
    plan = plan_orders(
        [book(top_other_ton=7.0, my_order_ton=8.0, my_order_id="O1")], policy()
    )

    assert plan.intents == []


def test_order_follows_the_floor_down():
    """Флор упал — наша заявка выше потолка и покупает без прибыли."""
    plan = plan_orders([book(floor_ton=5.0, my_order_ton=9.0, my_order_id="O1")], policy())
    intent = only(plan)

    assert intent.action is OrderAction.RAISE
    assert intent.price_ton == pytest.approx(4.5)
    assert "вниз" in intent.reason


def test_order_is_cancelled_when_collection_leaves_the_range():
    """Висящая мимо рынка заявка просто держит деньги в резерве."""
    plan = plan_orders(
        [book(floor_ton=50.0, my_order_ton=9.0, my_order_id="O1")],
        policy(max_floor_ton=4.0),
    )
    intent = only(plan)

    assert intent.action is OrderAction.CANCEL
    assert intent.order_id == "O1"


def test_order_is_cancelled_when_competitor_breaks_the_ceiling():
    plan = plan_orders(
        [book(top_other_ton=9.5, my_order_ton=8.0, my_order_id="O1")], policy()
    )

    assert only(plan).action is OrderAction.CANCEL


# --- Ограничение капитала -------------------------------------------------


def test_order_count_is_capped():
    books = [book(collection=f"C{i}", floor_ton=1.0 + i) for i in range(10)]
    plan = plan_orders(books, policy(max_orders=3))

    assert plan.placements == 3
    assert any("потолок" in reason for reason in plan.skipped.values())


def test_existing_orders_count_towards_the_cap():
    """Иначе движок держал бы вдвое больше заявок, чем разрешено."""
    books = [
        book(collection="A", floor_ton=1.0, my_order_ton=0.9, my_order_id="O1"),
        book(collection="B", floor_ton=2.0, my_order_ton=1.8, my_order_id="O2"),
        book(collection="C", floor_ton=3.0),
    ]
    plan = plan_orders(books, policy(max_orders=2))

    assert "C" in plan.skipped


def test_cheapest_collections_go_first():
    """При нехватке лимита дешёвые дают больше заявок на те же деньги."""
    books = [book(collection="Дорогая", floor_ton=9.0), book(collection="Дешёвая", floor_ton=1.0)]
    plan = plan_orders(books, policy(max_orders=1))

    assert only(plan).collection == "Дешёвая"


def test_amount_reaches_the_intent():
    intent = only(plan_orders([book()], policy(amount=3)))
    assert intent.amount == 3


def test_leading_order_is_not_lowered_to_save_pennies():
    """Мы первые с большим отрывом — заявку всё равно не двигаем.

    Перестановка отправляет в конец очереди, и ради доли процента можно
    не дождаться исполнения вовсе. Плюс защита от гонки вниз: двое
    подряд уступают друг другу, и не покупает никто.
    """
    plan = plan_orders(
        [book(top_other_ton=5.0, my_order_ton=8.5, my_order_id="O1")], policy()
    )

    assert plan.intents == []


def test_order_above_the_ceiling_is_lowered_even_while_leading():
    """Тут дело не в экономии: по такой заявке покупка убыточна."""
    plan = plan_orders(
        [book(floor_ton=5.0, top_other_ton=1.0, my_order_ton=8.0, my_order_id="O1")],
        policy(),
    )
    intent = only(plan)

    assert intent.action is OrderAction.RAISE
    assert intent.price_ton <= ceiling_price(5.0, policy()) + 1e-9
