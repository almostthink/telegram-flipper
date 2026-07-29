"""Риск-лимиты и предохранители.

Всё, что мешает одной ошибке стоить депозита. Проверки выполняются
непосредственно перед покупкой, а не при генерации сигнала: между этими
моментами могли открыться другие позиции.

Circuit breaker выключает автомат сам — без участия человека. Ошибки идут
сериями (сломался эндпоинт, кончился баланс, протух токен), и продолжать
торговать в такой момент означает множить убыток.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select

from app.config import RiskLimits
from app.domain import utcnow
from app.storage.db import session_scope
from app.storage.models import Position

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class RiskState:
    """Текущее состояние лимитов. Пересчитывается из базы, не копится в памяти."""

    spent_today_ton: float = 0.0
    realized_today_ton: float = 0.0
    open_positions: int = 0
    per_collection: dict[str, int] = field(default_factory=dict)
    consecutive_errors: int = 0
    tripped: bool = False
    trip_reason: str = ""


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.state = RiskState()

    def update_limits(self, limits: RiskLimits) -> None:
        self.limits = limits

    async def refresh(self, *, paper: bool) -> RiskState:
        """Пересчитываем расходы и позиции за сегодня."""
        since = utcnow() - timedelta(hours=24)

        async with session_scope() as session:
            spent = await session.scalar(
                select(func.coalesce(func.sum(Position.buy_price_ton), 0.0)).where(
                    Position.bought_at >= since, Position.paper.is_(paper)
                )
            )
            realized = await session.scalar(
                select(func.coalesce(func.sum(Position.net_pnl_ton), 0.0)).where(
                    Position.sold_at >= since, Position.paper.is_(paper)
                )
            )
            open_rows = list(
                (
                    await session.execute(
                        select(Position.collection).where(
                            Position.status == "open", Position.paper.is_(paper)
                        )
                    )
                ).scalars()
            )

        self.state.spent_today_ton = float(spent or 0.0)
        self.state.realized_today_ton = float(realized or 0.0)
        self.state.open_positions = len(open_rows)
        counts: dict[str, int] = {}
        for collection in open_rows:
            counts[collection] = counts.get(collection, 0) + 1
        self.state.per_collection = counts

        self._check_daily_loss()
        return self.state

    # --- Проверки перед покупкой ----------------------------------------

    def can_buy(self, collection: str, price_ton: float, *, auto: bool) -> RiskDecision:
        if self.state.tripped:
            return RiskDecision(False, f"предохранитель сработал: {self.state.trip_reason}")

        if price_ton > self.limits.max_position_ton:
            return RiskDecision(
                False,
                f"цена {price_ton:.2f} TON выше лимита на позицию "
                f"({self.limits.max_position_ton:.2f})",
            )

        if self.state.spent_today_ton + price_ton > self.limits.daily_budget_ton:
            remaining = self.limits.daily_budget_ton - self.state.spent_today_ton
            return RiskDecision(
                False, f"дневной бюджет исчерпан, осталось {max(remaining, 0):.2f} TON"
            )

        if self.state.open_positions >= self.limits.max_open_positions:
            return RiskDecision(
                False, f"открыто {self.state.open_positions} позиций — это максимум"
            )

        in_collection = self.state.per_collection.get(collection, 0)
        if in_collection >= self.limits.max_positions_per_collection:
            return RiskDecision(
                False,
                f"в коллекции уже {in_collection} позиций — защита от концентрации",
            )

        # В автоматическом режиме whitelist обязателен: пустой список
        # означает не «торгуй всем», а «не торгуй ничем».
        if auto:
            if not self.limits.collection_whitelist:
                return RiskDecision(False, "whitelist пуст — автомату торговать нечем")
            if collection not in self.limits.collection_whitelist:
                return RiskDecision(False, f"коллекция {collection} не в whitelist")

        if collection in self.limits.collection_blacklist:
            return RiskDecision(False, f"коллекция {collection} в blacklist")

        return RiskDecision(True)

    # --- Учёт результатов -----------------------------------------------

    def register_buy(self, collection: str, price_ton: float) -> None:
        self.state.spent_today_ton += price_ton
        self.state.open_positions += 1
        self.state.per_collection[collection] = self.state.per_collection.get(collection, 0) + 1

    def register_close(self, collection: str, net_pnl_ton: float) -> None:
        self.state.open_positions = max(self.state.open_positions - 1, 0)
        if collection in self.state.per_collection:
            self.state.per_collection[collection] = max(
                self.state.per_collection[collection] - 1, 0
            )
        self.state.realized_today_ton += net_pnl_ton
        self._check_daily_loss()

    def register_error(self) -> None:
        self.state.consecutive_errors += 1
        if self.state.consecutive_errors >= self.limits.circuit_breaker_errors:
            self.trip(f"{self.state.consecutive_errors} ошибок подряд")

    def register_success(self) -> None:
        self.state.consecutive_errors = 0

    def _check_daily_loss(self) -> None:
        loss = -self.state.realized_today_ton
        if loss >= self.limits.circuit_breaker_daily_loss_ton:
            self.trip(f"убыток за сутки {loss:.2f} TON")

    def trip(self, reason: str) -> None:
        if not self.state.tripped:
            log.error("ПРЕДОХРАНИТЕЛЬ: %s — автомат остановлен", reason)
        self.state.tripped = True
        self.state.trip_reason = reason

    def reset(self) -> None:
        """Сброс вручную из интерфейса — после того, как причина устранена."""
        self.state.tripped = False
        self.state.trip_reason = ""
        self.state.consecutive_errors = 0
        log.info("Предохранитель сброшен вручную")
