"""Баланс площадки: сколько денег реально доступно на покупки.

Подарки покупаются не с TON-кошелька напрямую, а с внутреннего депозита
площадки. У MRKT это ``totalHard`` в нанотонах из ``GET /api/v1/balance``;
кошелёк из ``/api/v1/wallet`` служит только для пополнения и вывода.

Замер кэшируется намеренно. Быстрая петля реагирует на свежий лот за
секунды, и лишний сетевой запрос перед каждой покупкой — это фора
конкурентам. Поэтому баланс снимается по расписанию, а списания между
замерами учитываются локально.

Неизвестный баланс покупку не блокирует. Заблокировать — значит из-за
единственной неудачи запроса тихо остановить всю торговлю; отказ площадки
и так придёт на самой покупке, но уже с внятной причиной.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.adapters import registry
from app.adapters.base import MarketplaceError
from app.config import Settings
from app.domain import Market, utcnow

log = logging.getLogger(__name__)

#: Через сколько секунд замер считается устаревшим. Дольше держать нельзя:
#: между замерами могли пройти наши же продажи и чужие списания.
STALE_SEC = 180.0


@dataclass
class MarketBalance:
    ton: float
    at: float
    #: Потрачено с момента замера — покупки быстрой петли между обновлениями.
    reserved: float = 0.0

    @property
    def available(self) -> float:
        return max(self.ton - self.reserved, 0.0)


@dataclass
class BalanceTracker:
    """Кэш балансов торгуемых площадок."""

    settings: Settings
    balances: dict[str, MarketBalance] = field(default_factory=dict)
    last_error: str = ""

    async def refresh(self) -> dict[str, float]:
        """Снимаем баланс со всех площадок, где разрешена торговля."""
        if self.settings.paper_mode:
            # В симуляции баланс не при чём: деньги не двигаются, а лишний
            # запрос к площадке в бумажном режиме — это след в её логах.
            self.balances.clear()
            return {}

        result: dict[str, float] = {}
        for name, cfg in self.settings.marketplaces.items():
            if not (cfg.enabled and cfg.trade_enabled):
                continue
            try:
                market = Market(name)
            except ValueError:
                continue

            adapter = registry.build_adapter(market, self.settings)
            try:
                async with adapter:
                    balance = await adapter.balance()
            except (MarketplaceError, NotImplementedError) as exc:
                self.last_error = f"{name}: {exc}"
                log.debug("Баланс %s недоступен: %s", name, exc)
                continue

            self.balances[name] = MarketBalance(ton=balance.ton, at=_now())
            result[name] = balance.ton

        return result

    def available(self, market: str) -> float | None:
        """Доступно к трате. None — замера нет или он устарел."""
        balance = self.balances.get(market)
        if balance is None or _now() - balance.at > STALE_SEC:
            return None
        return balance.available

    def reserve(self, market: str, price_ton: float) -> None:
        """Учитываем трату до следующего замера."""
        balance = self.balances.get(market)
        if balance is not None:
            balance.reserved += price_ton

    def shortfall(self, market: str, price_ton: float) -> str:
        """Причина отказа по деньгам. Пустая строка означает «хватает».

        Незнание баланса причиной отказа не считается — см. модуль.
        """
        available = self.available(market)
        if available is None or available >= price_ton:
            return ""
        return (
            f"на балансе {market} {available:.2f} TON, "
            f"а лот стоит {price_ton:.2f} TON"
        )

    @property
    def total_ton(self) -> float | None:
        """Суммарный доступный баланс. None — не измерен ни на одной площадке."""
        usable = [
            balance.available
            for name, balance in self.balances.items()
            if self.available(name) is not None
        ]
        return round(sum(usable), 3) if usable else None


def _now() -> float:
    return utcnow().timestamp()
