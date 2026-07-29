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

from app.analytics import pipeline, pricing
from app.analytics.signals import Signal
from app.api.ws import hub
from app.config import Settings
from app.domain import utcnow
from app.ingest.scanner import Scanner
from app.storage.db import session_scope
from app.storage.models import Position
from app.trading.executor import Executor
from app.trading.risk import RiskManager

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


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scanner = Scanner(settings)
        self.executor = Executor(settings)
        self.risk = RiskManager(settings.risk)
        self.last_signals: list[Signal] = []
        self.last_report = CycleReport()
        self.running = False

    def reload_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.scanner.settings = settings
        self.executor.settings = settings
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

    # --- Покупка ---------------------------------------------------------

    async def _auto_buy(self, signals: list[Signal], report: CycleReport) -> None:
        """Исполняем прошедшие сигналы под риск-лимитами."""
        if self.risk.state.tripped:
            report.blocked.append(f"предохранитель: {self.risk.state.trip_reason}")
            return

        for signal in signals:
            if not signal.passed:
                continue

            decision = self.risk.can_buy(
                signal.listing.collection, signal.listing.price_ton, auto=True
            )
            if not decision:
                report.blocked.append(f"{signal.listing.collection}: {decision.reason}")
                continue

            result = await self.executor.buy(
                signal.listing,
                fair_value=signal.fair.value_ton,
                expected_tts=signal.liquidity.expected_tts_hours,
                reason=signal.explanation,
            )

            if result.ok:
                self.risk.register_buy(signal.listing.collection, signal.listing.price_ton)
                self.risk.register_success()
                report.bought += 1
                await hub.broadcast(
                    "position_opened",
                    {
                        "collection": signal.listing.collection,
                        "price_ton": signal.listing.price_ton,
                        "roi": signal.net_roi,
                    },
                )
            else:
                self.risk.register_error()
                report.errors.append(result.detail)
                if self.risk.state.tripped:
                    break

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
        decision = self.risk.can_buy(
            signal.listing.collection, signal.listing.price_ton, auto=False
        )
        if not decision:
            return False, decision.reason

        result = await self.executor.buy(
            signal.listing,
            fair_value=signal.fair.value_ton,
            expected_tts=signal.liquidity.expected_tts_hours,
            reason=f"вручную · {signal.explanation}",
        )
        if result.ok:
            self.risk.register_buy(signal.listing.collection, signal.listing.price_ton)
        return result.ok, result.detail

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
            "disabled_markets": self.scanner.disabled_markets,
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
