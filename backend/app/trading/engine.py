"""Торговый движок: сканирование, сигналы, покупка, сопровождение позиций.

Работает циклами по расписанию. В полуавтоматическом режиме доходит до
сигналов и останавливается — покупку инициирует человек. В автоматическом
дополнительно исполняет покупки, но только под риск-лимитами.

Сопровождение позиций работает всегда, независимо от режима: купленный
подарок надо выставить и постепенно снижать цену, иначе он зависнет.

Лестница переоценки: выставляем с наценкой к справедливой цене, затем
каждые ``reprice_interval_hours`` снижаем на ``reprice_step``, но не ниже
порога безубытка. По истечении ``max_hold_hours`` сбрасываем по
консервативной цене — зависшая позиция хуже небольшого убытка, потому что
блокирует капитал.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from sqlalchemy import select

from app.adapters import registry
from app.adapters.base import MarketplaceError
from app.analytics import pipeline, pricing
from app.analytics.signals import Signal
from app.api.ws import hub
from app.config import Settings
from app.domain import Market, utcnow
from app.ingest.scanner import Scanner
from app.storage import repo
from app.storage.db import session_scope
from app.storage.models import Position
from app.trading import orders as orders_mod
from app.trading.executor import Executor
from app.trading.orders import OrderPolicy
from app.trading.risk import RiskManager
from app.trading.wallet import BalanceTracker

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    signals_total: int = 0
    signals_passed: int = 0
    bought: int = 0
    listed: int = 0
    repriced: int = 0
    closed: int = 0
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "signals_total": self.signals_total,
            "signals_passed": self.signals_passed,
            "bought": self.bought,
            "listed": self.listed,
            "repriced": self.repriced,
            "closed": self.closed,
            "blocked": self.blocked[:10],
            "errors": self.errors[:10],
        }


@dataclass
class OrderReport:
    """Итог одного пересмотра заявок."""

    considered: int = 0
    placed: int = 0
    cancelled: int = 0
    simulated: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "placed": self.placed,
            "cancelled": self.cancelled,
            "simulated": self.simulated,
            "skipped": dict(list(self.skipped.items())[:20]),
            "log": self.log[-40:],
            "errors": self.errors[:10],
        }


@dataclass
class WatchReport:
    """Итог одного тика быстрой петли."""

    #: Сколько лотов появилось на рынке с прошлого тика.
    seen: int = 0
    #: Из них дошло до оценки — те, чья коллекция уже отслеживается.
    evaluated: int = 0
    passed: int = 0
    bought: int = 0
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "seen": self.seen,
            "evaluated": self.evaluated,
            "passed": self.passed,
            "bought": self.bought,
            "blocked": self.blocked[:10],
            "errors": self.errors[:10],
        }


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scanner = Scanner(settings)
        self.executor = Executor(settings)
        self.risk = RiskManager(settings.risk)
        self.wallet = BalanceTracker(settings)
        self.last_signals: list[Signal] = []
        self.last_report = CycleReport()
        self.last_watch = WatchReport()
        self.last_orders = OrderReport()
        self.running = False
        self.watching = False
        self.ordering = False

    def reload_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.scanner.settings = settings
        self.executor.settings = settings
        self.wallet.settings = settings
        self.risk.update_limits(settings.risk)

    # --- Основной цикл ---------------------------------------------------

    async def run_cycle(self, *, deep: bool = True) -> CycleReport:
        """Один полный проход: собрать данные, оценить, действовать."""
        if self.running:
            log.debug("Цикл уже выполняется — пропускаю")
            return self.last_report

        self.running = True
        report = CycleReport()
        try:
            scan = await self.scanner.scan_floors()
            if deep:
                scan.merge(await self.scanner.scan_collections())
            report.errors.extend(scan.errors)

            signals = await pipeline.evaluate_all(self.settings)
            await pipeline.persist(signals)
            self.last_signals = signals
            report.signals_total = len(signals)
            report.signals_passed = sum(1 for signal in signals if signal.passed)

            await self.risk.refresh(paper=self.settings.paper_mode)
            await self.wallet.refresh()

            if self.settings.auto_trade:
                await self._auto_buy(signals, report)

            await self._manage_positions(report)

            if self.settings.paper_mode:
                closed = await self.executor.settle_paper_positions()
                report.closed += len(closed)

        except Exception as exc:  # noqa: BLE001 — движок не должен падать целиком
            log.exception("Ошибка торгового цикла")
            report.errors.append(str(exc))
            self.risk.register_error()
        finally:
            self.running = False
            self.last_report = report

        await hub.broadcast("cycle", report.as_dict())
        return report

    # --- Ордер-движок ----------------------------------------------------

    async def run_orders(self) -> OrderReport:
        """Пересмотр заявок на покупку.

        Второй способ набора позиции, обратный охоте за листингами: не
        ждём чужой ошибки, а сами встаём в очередь покупателей ниже флора.
        Исполненная заявка даёт подарок в инвентарь, и дальше он идёт по
        обычному циклу перепродажи.

        Живая заявка — это живые деньги: площадка резервирует их сразу, до
        всякого исполнения. Поэтому вне бумажного режима движок подчиняется
        тем же предохранителям, что и покупка, — тумблеру автомата, белому
        списку и лимитам. Иначе включённый ордер-движок оказался бы обходной
        дорогой мимо выключенного автомата.
        """
        report = OrderReport()
        if self.ordering:
            return self.last_orders

        if not self.settings.paper_mode and not self.settings.auto_trade:
            report.skipped["автомат"] = (
                "выключен — живые заявки не ставим. Включите автомат "
                "или оставьте paper-режим"
            )
            self.last_orders = report
            return report

        self.ordering = True
        try:
            if not self.settings.paper_mode:
                # Баланс нужен до первой заявки: без него нечем ограничить
                # суммарный объём, а петля работает отдельно от цикла.
                await self.wallet.refresh()
            for market in registry.trading_markets(self.settings):
                await self._run_orders_on(market, report)
        except Exception as exc:  # noqa: BLE001 — движок не должен падать
            log.exception("Ошибка ордер-движка")
            report.errors.append(str(exc))
        finally:
            self.ordering = False
            self.last_orders = report

        return report

    async def _run_orders_on(self, market: Market, report: OrderReport) -> None:
        cfg = self.settings.orders
        adapter = registry.build_adapter(market, self.settings)
        fee = self.settings.marketplaces[market.value].fee_sell

        policy = OrderPolicy(
            target_spread=cfg.target_spread,
            min_floor_ton=cfg.min_floor_ton,
            max_floor_ton=cfg.max_floor_ton,
            amount=cfg.amount,
            max_orders=cfg.max_orders,
            tick_ton=cfg.tick_ton,
            fee_sell=fee,
        )

        try:
            async with adapter:
                mine = await adapter.my_orders()
                tops = await adapter.top_offers()
                books = await self._build_books(market, mine, tops)
                plan = orders_mod.plan_orders(books, policy)
                report.considered += len(books)
                report.skipped.update(plan.skipped)
                await self._apply_orders(adapter, plan, report, market)
        except MarketplaceError as exc:
            report.errors.append(f"{market.value}: {exc}")

    async def _build_books(
        self, market: Market, mine: list, tops: list
    ) -> list[orders_mod.CollectionBook]:
        """Сводим флоры, чужие верхние заявки и свои заявки в одну картину."""
        async with session_scope() as session:
            floors = await repo.collection_floor_map(session)

        my_by_collection = {item.collection: item for item in mine}
        # Верхняя заявка площадки может быть нашей же: перебивать себя
        # означало бы поднимать цену против пустоты.
        top_by_collection: dict[str, float] = {}
        for offer in tops:
            own = my_by_collection.get(offer.collection)
            if own is not None and abs(own.price_ton - offer.price_ton) < 1e-9:
                continue
            if offer.price_ton > top_by_collection.get(offer.collection, 0.0):
                top_by_collection[offer.collection] = offer.price_ton

        books = []
        for collection, floor in floors.items():
            own = my_by_collection.get(collection)
            books.append(
                orders_mod.CollectionBook(
                    collection=collection,
                    floor_ton=floor,
                    top_other_ton=top_by_collection.get(collection),
                    my_order_ton=own.price_ton if own else None,
                    my_order_id=own.order_id if own else None,
                    amount=own.amount if own else self.settings.orders.amount,
                )
            )
        return books

    def _order_blocked(self, intent, market: Market) -> str:
        """Причина, по которой живую заявку ставить нельзя. Пусто — можно.

        Проверки те же, что у покупки: заявка обещает деньги так же, как
        покупка их тратит, и площадка резервирует их сразу.
        """
        limits = self.settings.risk

        if self.risk.state.tripped:
            return f"предохранитель: {self.risk.state.trip_reason}"
        if intent.collection in limits.collection_blacklist:
            return "коллекция в blacklist"
        if not limits.collection_whitelist:
            return "whitelist пуст — автомату торговать нечем"
        if intent.collection not in limits.collection_whitelist:
            return "коллекция не в whitelist"

        cost = intent.price_ton * max(intent.amount, 1)
        if intent.price_ton > limits.max_position_ton:
            return (
                f"цена {intent.price_ton:.2f} TON выше лимита на позицию "
                f"({limits.max_position_ton:.2f})"
            )
        return self.wallet.shortfall(market.value, cost)

    async def _apply_orders(self, adapter, plan, report: OrderReport, market: Market) -> None:
        """Исполняем план. В бумажном режиме только считаем — денег не двигаем."""
        for intent in plan.intents:
            if self.settings.paper_mode:
                report.simulated += 1
                report.log.append(f"[PAPER] {intent.action} {intent.collection} "
                                  f"{intent.price_ton:.2f} TON — {intent.reason}")
                continue

            # Снятие заявки не проверяем: оно освобождает деньги, а не
            # обещает их. Запрещать его предохранителем — значит запирать
            # капитал именно тогда, когда что-то пошло не так.
            if intent.action is not orders_mod.OrderAction.CANCEL:
                blocked = self._order_blocked(intent, market)
                if blocked:
                    report.skipped[intent.collection] = blocked
                    continue

            try:
                if intent.action is orders_mod.OrderAction.CANCEL:
                    await adapter.cancel_order(intent.order_id or "")
                    report.cancelled += 1
                else:
                    # Поднятие цены — это снятие и постановка заново:
                    # менять цену стоящей заявки площадка не умеет.
                    if intent.order_id:
                        await adapter.cancel_order(intent.order_id)
                    await adapter.create_order(
                        intent.collection, intent.price_ton, intent.amount
                    )
                    report.placed += 1
                    # Иначе весь план пройдёт одну и ту же проверку баланса
                    # и наобещает больше, чем есть денег.
                    self.wallet.reserve(market.value, intent.price_ton * max(intent.amount, 1))
                report.log.append(
                    f"{intent.action} {intent.collection} "
                    f"{intent.price_ton:.2f} TON — {intent.reason}"
                )
            except MarketplaceError as exc:
                report.errors.append(f"{intent.collection}: {exc}")

    # --- Быстрая петля ---------------------------------------------------

    async def run_watch(self) -> WatchReport:
        """Реакция на свежие лоты.

        Плановый цикл ходит раз в двадцать минут — для флиппинга это
        вечность: недооценённый лот разбирают за секунды. Здесь путь
        короткий: один запрос к ленте, пересчёт только затронутых
        коллекций, покупка через те же проверки, что и в основном цикле.

        Оценка опирается на данные, собранные медленными петлями: флоры,
        флоры атрибутов, историю сделок. Быстрая петля их не собирает —
        она только замечает новое предложение и прикладывает к нему уже
        готовую модель.
        """
        report = WatchReport()
        if self.watching:
            # Предыдущий тик ещё идёт: рынок ушёл вперёд, догонять незачем.
            return self.last_watch

        self.watching = True
        try:
            scan, fresh = await self.scanner.scan_new_listings()
            report.seen = scan.new_listings
            report.errors.extend(scan.errors)
            if not fresh:
                return report

            signals: list[Signal] = []
            for collection, listing_ids in fresh.items():
                wanted = set(listing_ids)
                try:
                    evaluated = await pipeline.evaluate_one(self.settings, collection)
                except Exception as exc:  # noqa: BLE001 — одна коллекция не роняет тик
                    log.exception("Быстрая оценка %s не удалась", collection)
                    report.errors.append(f"{collection}: {exc}")
                    continue
                # Из коллекции берём только те лоты, которые появились
                # сейчас: остальные уже рассматривались плановым циклом.
                signals.extend(
                    signal
                    for signal in evaluated
                    if signal.listing.listing_id in wanted
                )

            report.evaluated = len(signals)
            passed = [signal for signal in signals if signal.passed]
            report.passed = len(passed)
            if passed:
                await pipeline.persist(passed)
                self.last_signals = passed + self.last_signals[:200]

            if passed and self.settings.auto_trade:
                await self._buy_fresh(passed, report)

        except Exception as exc:  # noqa: BLE001 — петля не должна падать
            log.exception("Ошибка быстрой петли")
            report.errors.append(str(exc))
        finally:
            self.watching = False
            self.last_watch = report

        if report.bought:
            await hub.broadcast("watch", report.as_dict())
        return report

    async def _buy_fresh(self, signals: list[Signal], report: WatchReport) -> None:
        if self.risk.state.tripped:
            report.blocked.append(f"предохранитель: {self.risk.state.trip_reason}")
            return

        # Сначала самые выгодные: на всех денег всё равно не хватит.
        for signal in sorted(signals, key=lambda s: s.score, reverse=True):
            ok, detail = await self.attempt_buy(signal, auto=True, note="быстрая петля")
            if ok:
                report.bought += 1
            elif detail:
                report.blocked.append(f"{signal.listing.collection}: {detail}")
                if self.risk.state.tripped:
                    break

    # --- Покупка ---------------------------------------------------------

    async def _auto_buy(self, signals: list[Signal], report: CycleReport) -> None:
        """Исполняем прошедшие сигналы под риск-лимитами."""
        if self.risk.state.tripped:
            report.blocked.append(f"предохранитель: {self.risk.state.trip_reason}")
            return

        for signal in signals:
            if not signal.passed:
                continue

            ok, detail = await self.attempt_buy(signal, auto=True)
            if ok:
                report.bought += 1
            elif detail:
                report.blocked.append(f"{signal.listing.collection}: {detail}")
                if self.risk.state.tripped:
                    break

    async def attempt_buy(
        self, signal: Signal, *, auto: bool, note: str = ""
    ) -> tuple[bool, str]:
        """Одна попытка покупки под всеми проверками.

        Общая точка для планового цикла, быстрой петли и кнопки в
        интерфейсе: проверки должны быть одни и те же, иначе быстрый путь
        рано или поздно разойдётся с медленным и обойдёт лимит.
        """
        listing = signal.listing

        decision = self.risk.can_buy(listing.collection, listing.price_ton, auto=auto)
        if not decision:
            return False, decision.reason

        shortfall = self.wallet.shortfall(listing.market, listing.price_ton)
        if shortfall:
            return False, shortfall

        reason = signal.explanation if not note else f"{note} · {signal.explanation}"
        result = await self.executor.buy(
            listing,
            fair_value=signal.fair.value_ton,
            expected_tts=signal.liquidity.expected_tts_hours,
            reason=reason,
        )

        if not result.ok:
            self.risk.register_error()
            return False, result.detail

        self.risk.register_buy(listing.collection, listing.price_ton)
        self.risk.register_success()
        self.wallet.reserve(listing.market, listing.price_ton)
        await hub.broadcast(
            "position_opened",
            {
                "collection": listing.collection,
                "price_ton": listing.price_ton,
                "roi": signal.net_roi,
            },
        )
        return True, result.detail

    async def buy_manually(self, listing_id: str, market: str) -> tuple[bool, str]:
        """Покупка по кнопке из интерфейса.

        Риск-лимиты действуют и здесь, кроме whitelist: если человек
        осознанно выбрал лот, ограничивать его списком коллекций незачем.
        """
        signal = next(
            (
                candidate
                for candidate in self.last_signals
                if candidate.listing.listing_id == listing_id
                and candidate.listing.market == market
            ),
            None,
        )
        if signal is None:
            return False, "сигнал не найден или устарел — обновите список"

        await self.risk.refresh(paper=self.settings.paper_mode)
        await self.wallet.refresh()
        return await self.attempt_buy(signal, auto=False, note="вручную")

    # --- Сопровождение позиций -------------------------------------------

    async def _manage_positions(self, report: CycleReport) -> None:
        async with session_scope() as session:
            positions = list(
                (
                    await session.execute(
                        select(Position).where(
                            Position.status.in_(("open", "listed")),
                            Position.paper.is_(self.settings.paper_mode),
                        )
                    )
                ).scalars()
            )

        for position in positions:
            try:
                if position.status == "open":
                    if await self._list_new(position):
                        report.listed += 1
                elif await self._maybe_reprice(position):
                    report.repriced += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка сопровождения позиции %d", position.id)
                report.errors.append(f"позиция {position.id}: {exc}")

    async def _list_new(self, position: Position) -> bool:
        """Выставляем свежекупленную позицию с наценкой."""
        fair = position.fair_value_at_buy or position.buy_price_ton
        price = fair * (1 + self.settings.sell.initial_markup)
        price = max(price, self._breakeven_price(position))
        result = await self.executor.list_for_sale(position, round(price, 3))
        return result.ok

    async def _maybe_reprice(self, position: Position) -> bool:
        """Шаг лестницы переоценки, если пришло время."""
        now = utcnow()
        last = _aware(position.last_repriced_at or position.bought_at)
        held_hours = (now - _aware(position.bought_at)).total_seconds() / 3600
        since_last = (now - last).total_seconds() / 3600

        if position.ask_price_ton is None:
            return False

        breakeven = self._breakeven_price(position)

        # Время вышло: сбрасываем ниже безубытка. Зависшая позиция блокирует
        # капитал, а капитал — единственный рабочий ресурс флиппера.
        if held_hours >= self.settings.sell.max_hold_hours:
            target = position.buy_price_ton * 0.92
            if position.ask_price_ton <= target:
                return False
            result = await self.executor.reprice(
                position,
                round(target, 3),
                f"держим {held_hours:.0f}ч при лимите "
                f"{self.settings.sell.max_hold_hours:.0f}ч — сброс по бид-сайду",
            )
            return result.ok

        if since_last < self.settings.sell.reprice_interval_hours:
            return False

        target = position.ask_price_ton * (1 - self.settings.sell.reprice_step)
        if target <= breakeven:
            # Ниже порога безубытка по расписанию не опускаемся — только
            # по истечении max_hold, и это отдельное осознанное решение.
            if position.ask_price_ton <= breakeven * 1.001:
                return False
            target = breakeven

        result = await self.executor.reprice(
            position,
            round(target, 3),
            f"шаг лестницы: держим {held_hours:.0f}ч, "
            f"снижение на {self.settings.sell.reprice_step * 100:.0f}%",
        )
        return result.ok

    def _breakeven_price(self, position: Position) -> float:
        """Цена, ниже которой сделка перестаёт окупать комиссии."""
        cfg = self.settings.marketplaces.get(position.market)
        fee_sell = cfg.fee_sell if cfg else 0.05
        fee_buy = cfg.fee_buy if cfg else 0.0
        gas = self.settings.analytics.gas_ton
        cost = position.buy_price_ton * (1 + fee_buy) + 2 * gas
        margin = 1 + self.settings.sell.floor_margin
        return cost * margin / max(1 - fee_sell, 0.01)

    # --- Управление ------------------------------------------------------

    def kill_switch(self) -> None:
        """Мгновенная остановка автомата."""
        self.settings.auto_trade = False
        self.risk.trip("остановлено вручную")
        self.settings.save()
        log.warning("Kill-switch: автоматическая торговля выключена")

    def status(self) -> dict:
        return {
            "running": self.running,
            "paper_mode": self.settings.paper_mode,
            "auto_trade": self.settings.auto_trade,
            "risk": {
                "spent_today_ton": round(self.risk.state.spent_today_ton, 3),
                "realized_today_ton": round(self.risk.state.realized_today_ton, 3),
                "open_positions": self.risk.state.open_positions,
                "daily_budget_ton": self.settings.risk.daily_budget_ton,
                "max_open_positions": self.settings.risk.max_open_positions,
                "tripped": self.risk.state.tripped,
                "trip_reason": self.risk.state.trip_reason,
                "consecutive_errors": self.risk.state.consecutive_errors,
            },
            "balance_ton": self.wallet.total_ton,
            "disabled_markets": self.scanner.disabled_markets,
            "watch_enabled": self.settings.watch_enabled,
            "watch_interval_sec": self.settings.watch_interval_sec,
            "last_watch": self.last_watch.as_dict(),
            "orders_enabled": self.settings.orders.enabled,
            "orders_interval_sec": self.settings.orders.interval_sec,
            "last_orders": self.last_orders.as_dict(),
            "last_cycle": self.last_report.as_dict(),
            "breakeven_markup": round(
                pricing.breakeven_markup(
                    self.settings.marketplaces["portals"].fee_sell
                    if "portals" in self.settings.marketplaces
                    else 0.05
                ),
                4,
            ),
        }


def _aware(value):
    if value is None:
        return utcnow() - timedelta(days=1)
    return value if value.tzinfo else value.replace(tzinfo=UTC)
