"""Ордер-движок: заявки на покупку в пределах спреда от флора.

Логика противоположна отбору листингов. Там мы ищем чужую ошибку —
кто-то выставил дешевле рынка, и надо успеть первым. Здесь ошибки никто
не делает: мы сами встаём в очередь покупателей чуть ниже флора и ждём,
пока продавец согласится отдать по нашей цене. Заявка исполняется —
подарок попадает в инвентарь и уходит в обычный цикл перепродажи.

Три правила, из которых состоит всё остальное:

**Потолок.** Заявка никогда не выше ``флор × (1 − спред)``. Это и есть
источник прибыли: купить ниже флора, продать у флора. Спред меньше
комиссии площадки бессмысленен — на нём заработать нельзя.

**Перебивание.** Чтобы стоять первым, надо дать больше текущей верхней
заявки, но не выше потолка. Если конкурент уже перебил потолок, значит
он торгует без запаса, и ввязываться незачем: мы просто уходим.

**Отказ от лишнего.** Заявка, вышедшая за рамки — из-за упавшего флора,
из-за конкурента, из-за смены настроек, — снимается. Заявка стоит денег:
площадка резервирует их на балансе, и висящая мимо рынка заявка просто
замораживает капитал.

Модуль ничего не знает о площадках: на вход — состояние рынка, на выход —
список действий. Из-за этого он целиком проверяется тестами, а тонкости
конкретных API остаются в адаптерах.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger(__name__)

#: Шаг перебивания. Меньше — дольше стоим на месте, больше — переплата
#: при каждом обновлении. Сотая доля TON заметно ниже любого разумного
#: спреда и при этом различима для площадки.
DEFAULT_TICK_TON = 0.01


class OrderAction(StrEnum):
    PLACE = "place"
    RAISE = "raise"
    CANCEL = "cancel"


@dataclass(slots=True)
class CollectionBook:
    """Состояние одной коллекции глазами движка."""

    collection: str
    floor_ton: float
    #: Верхняя чужая заявка. None — заявок нет вовсе, очередь пуста.
    top_other_ton: float | None = None
    #: Наша заявка, если она уже стоит.
    my_order_ton: float | None = None
    my_order_id: str | None = None
    #: Сколько подарков просим по этой заявке.
    amount: int = 1


@dataclass(slots=True)
class OrderIntent:
    """Одно намерение движка. Исполняет его адаптер площадки."""

    action: OrderAction
    collection: str
    price_ton: float = 0.0
    amount: int = 1
    order_id: str | None = None
    reason: str = ""


@dataclass(slots=True)
class OrderPolicy:
    """Правила расстановки заявок."""

    #: Насколько ниже флора держим заявку. 0.01 — один процент.
    target_spread: float = 0.01
    #: Коллекции с флором вне этих границ не трогаем вовсе.
    min_floor_ton: float = 0.0
    max_floor_ton: float = 4.0
    #: Сколько подарков просить в одной заявке.
    amount: int = 1
    #: Потолок числа одновременных заявок — прямое ограничение капитала.
    max_orders: int = 20
    #: Шаг перебивания конкурента.
    tick_ton: float = DEFAULT_TICK_TON
    #: Комиссия продажи. Спред ниже неё гарантированно убыточен.
    fee_sell: float = 0.0


@dataclass(slots=True)
class OrderPlan:
    intents: list[OrderIntent] = field(default_factory=list)
    #: Почему коллекция пропущена: имя → причина. Нужно для интерфейса,
    #: иначе движок молчит и непонятно, чего он ждёт.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def placements(self) -> int:
        return sum(1 for item in self.intents if item.action is not OrderAction.CANCEL)


def ceiling_price(floor_ton: float, policy: OrderPolicy) -> float:
    """Максимум, который мы готовы дать: флор минус спред."""
    return floor_ton * (1.0 - policy.target_spread)


def plan_orders(books: list[CollectionBook], policy: OrderPolicy) -> OrderPlan:
    """Что сделать с заявками прямо сейчас.

    Порядок важен: сначала решаем судьбу уже стоящих заявок, потом
    добираем новые. Иначе снятие и постановка спорят за один и тот же
    лимит ``max_orders``, и движок то ставит, то снимает по кругу.
    """
    plan = OrderPlan()
    if policy.target_spread <= policy.fee_sell:
        # Купить со скидкой меньше комиссии и продать у флора — это
        # заплатить площадке за свою работу.
        plan.skipped["*"] = (
            f"спред {policy.target_spread * 100:.2f}% не покрывает комиссию "
            f"{policy.fee_sell * 100:.2f}% — прибыли не будет ни на одной заявке"
        )
        return plan

    keep = 0
    for book in sorted(books, key=lambda item: item.floor_ton):
        decision, reason = _decide(book, policy)

        if decision is None:
            if book.my_order_ton is not None:
                plan.intents.append(
                    OrderIntent(
                        action=OrderAction.CANCEL,
                        collection=book.collection,
                        order_id=book.my_order_id,
                        reason=reason,
                    )
                )
            plan.skipped[book.collection] = reason
            continue

        if book.my_order_ton is None:
            if keep >= policy.max_orders:
                plan.skipped[book.collection] = (
                    f"достигнут потолок в {policy.max_orders} заявок"
                )
                continue
            keep += 1
            plan.intents.append(
                OrderIntent(
                    action=OrderAction.PLACE,
                    collection=book.collection,
                    price_ton=decision,
                    amount=policy.amount,
                    reason=reason,
                )
            )
            continue

        keep += 1
        ceiling = ceiling_price(book.floor_ton, policy)

        if decision > book.my_order_ton + 1e-9:
            # Нас перебили — поднимаемся.
            plan.intents.append(
                OrderIntent(
                    action=OrderAction.RAISE,
                    collection=book.collection,
                    price_ton=decision,
                    amount=book.amount or policy.amount,
                    order_id=book.my_order_id,
                    reason=reason,
                )
            )
        elif book.my_order_ton > ceiling + 1e-9:
            # Флор ушёл вниз, и наша заявка оказалась выше потолка: по ней
            # мы купим без запаса на комиссию. Опускаем.
            plan.intents.append(
                OrderIntent(
                    action=OrderAction.RAISE,
                    collection=book.collection,
                    price_ton=decision,
                    amount=book.amount or policy.amount,
                    order_id=book.my_order_id,
                    reason=f"{reason} (флор ушёл вниз)",
                )
            )
        # Иначе заявку не трогаем, даже когда могли бы стоять дешевле.
        # Экономия тут копеечная, а перестановка заявки отправляет нас в
        # конец очереди — и исполнения можно не дождаться вовсе. Заодно
        # это защищает от гонки вниз, когда двое подряд уступают друг
        # другу и в итоге не покупает никто.

    return plan


def _decide(book: CollectionBook, policy: OrderPolicy) -> tuple[float | None, str]:
    """Цена нашей заявки по коллекции или None, если ввязываться не стоит."""
    if book.floor_ton <= 0:
        return None, "нет флора"

    if book.floor_ton < policy.min_floor_ton:
        return None, f"флор {book.floor_ton:.2f} ниже нижней границы"
    if book.floor_ton > policy.max_floor_ton:
        return None, f"флор {book.floor_ton:.2f} выше верхней границы"

    ceiling = ceiling_price(book.floor_ton, policy)
    if ceiling <= 0:
        return None, "спред съедает всю цену"

    if book.top_other_ton is None:
        # Очередь пуста — незачем сразу давать максимум. Встаём у потолка
        # только когда есть с кем спорить.
        return round(ceiling, 4), "очередь пуста, встаём по потолку спреда"

    contender = book.top_other_ton + policy.tick_ton
    if contender > ceiling + 1e-9:
        return None, (
            f"конкурент даёт {book.top_other_ton:.2f} — это выше нашего "
            f"потолка {ceiling:.2f}, запаса не остаётся"
        )

    return round(contender, 4), f"перебиваем {book.top_other_ton:.2f}"
