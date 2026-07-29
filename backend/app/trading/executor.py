"""Исполнение сделок — реальное и симулированное.

Один интерфейс на оба режима. Торговый движок не знает, идут ли деньги
на самом деле: это решает executor по флагу ``paper``. Так симуляция и
живая торговля гарантированно проходят один и тот же код, и paper-режим
проверяет именно то, что потом будет исполняться.

В paper-режиме продажа не «происходит» по желанию, а моделируется по
данным рынка: позиция закрывается, когда наблюдаемые сделки прошли по
цене не ниже нашего аска. Иначе симуляция показывала бы 100% win rate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select

from app.adapters import registry
from app.adapters.base import Marketplace, MarketplaceError
from app.config import Settings
from app.domain import Listing, Market, utcnow
from app.storage.db import session_scope
from app.storage.models import ListingSnapshot, Position, SaleRecord, TradeLog

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    detail: str
    position_id: int | None = None
    reference: str | None = None


class Executor:
    """Покупка, выставление на продажу и переоценка."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def paper(self) -> bool:
        return self.settings.paper_mode

    # --- Покупка ---------------------------------------------------------

    async def buy(
        self,
        listing: ListingSnapshot,
        *,
        fair_value: float | None = None,
        expected_tts: float | None = None,
        reason: str = "",
    ) -> ExecutionResult:
        market = Market(listing.market)
        reference: str | None = None

        if not self.paper:
            adapter = registry.build_adapter(market, self.settings)
            domain_listing = Listing(
                market=market,
                listing_id=listing.listing_id,
                gift=_gift_from_snapshot(listing),
                price_ton=listing.price_ton,
            )
            try:
                async with adapter:
                    reference = await adapter.buy(domain_listing)
            except MarketplaceError as exc:
                await self._log(None, "buy", market.value, listing.price_ton, str(exc), ok=False)
                return ExecutionResult(False, f"покупка не удалась: {exc}")

        async with session_scope() as session:
            position = Position(
                market=listing.market,
                gift_external_id=listing.listing_id,
                collection=listing.collection,
                model=listing.model,
                backdrop=listing.backdrop,
                symbol=listing.symbol,
                buy_price_ton=listing.price_ton,
                fair_value_at_buy=fair_value,
                expected_tts_hours=expected_tts,
                reason=reason,
                status="open",
                paper=self.paper,
            )
            session.add(position)
            await session.flush()
            session.add(
                TradeLog(
                    position_id=position.id,
                    action="buy",
                    market=listing.market,
                    price_ton=listing.price_ton,
                    detail=reason or "покупка по сигналу",
                    ok=True,
                    paper=self.paper,
                )
            )
            position_id = position.id

        log.info(
            "%s куплен %s за %.2f TON (позиция %d)",
            "[PAPER]" if self.paper else "[LIVE]",
            listing.collection,
            listing.price_ton,
            position_id,
        )
        return ExecutionResult(True, "куплено", position_id=position_id, reference=reference)

    # --- Выставление на продажу -------------------------------------------

    async def list_for_sale(self, position: Position, price_ton: float) -> ExecutionResult:
        market = Market(position.market)
        listing_id = position.gift_external_id

        if not self.paper:
            adapter = registry.build_adapter(market, self.settings)
            try:
                async with adapter:
                    listing_id = await adapter.list_for_sale(
                        position.gift_external_id, price_ton
                    )
            except MarketplaceError as exc:
                await self._log(
                    position.id, "list", market.value, price_ton, str(exc), ok=False
                )
                return ExecutionResult(False, f"выставление не удалось: {exc}")

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is None:
                return ExecutionResult(False, "позиция не найдена")
            stored.listing_id = listing_id
            stored.ask_price_ton = price_ton
            stored.status = "listed"
            session.add(
                TradeLog(
                    position_id=stored.id,
                    action="list",
                    market=stored.market,
                    price_ton=price_ton,
                    detail=f"выставлено за {price_ton:.2f} TON",
                    paper=self.paper,
                )
            )

        return ExecutionResult(True, f"выставлено за {price_ton:.2f} TON", position.id)

    async def reprice(self, position: Position, price_ton: float, reason: str) -> ExecutionResult:
        market = Market(position.market)

        if not self.paper and position.listing_id:
            adapter = registry.build_adapter(market, self.settings)
            try:
                async with adapter:
                    await adapter.change_price(position.listing_id, price_ton)
            except MarketplaceError as exc:
                await self._log(
                    position.id, "reprice", market.value, price_ton, str(exc), ok=False
                )
                return ExecutionResult(False, f"переоценка не удалась: {exc}")

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is None:
                return ExecutionResult(False, "позиция не найдена")
            stored.ask_price_ton = price_ton
            stored.reprice_count += 1
            stored.last_repriced_at = utcnow()
            session.add(
                TradeLog(
                    position_id=stored.id,
                    action="reprice",
                    market=stored.market,
                    price_ton=price_ton,
                    detail=reason,
                    paper=self.paper,
                )
            )

        return ExecutionResult(True, f"цена снижена до {price_ton:.2f} TON", position.id)

    # --- Закрытие --------------------------------------------------------

    async def close(
        self, position_id: int, sell_price_ton: float, detail: str
    ) -> ExecutionResult:
        """Фиксируем продажу и считаем чистый результат."""
        fee_sell = self._fee_sell(None)
        gas = self.settings.analytics.gas_ton

        async with session_scope() as session:
            stored = await session.get(Position, position_id)
            if stored is None:
                return ExecutionResult(False, "позиция не найдена")

            fee_sell = self._fee_sell(stored.market)
            proceeds = sell_price_ton * (1 - fee_sell) - gas
            cost = stored.buy_price_ton * (1 + self._fee_buy(stored.market)) + gas

            stored.sell_price_ton = sell_price_ton
            stored.sold_at = utcnow()
            stored.net_pnl_ton = round(proceeds - cost, 6)
            stored.status = "closed"
            session.add(
                TradeLog(
                    position_id=stored.id,
                    action="sell",
                    market=stored.market,
                    price_ton=sell_price_ton,
                    detail=f"{detail} · чистыми {stored.net_pnl_ton:+.3f} TON",
                    paper=stored.paper,
                )
            )
            pnl = stored.net_pnl_ton
            collection = stored.collection

        log.info(
            "Позиция %d закрыта: %s за %.2f TON, чистыми %+.3f",
            position_id, collection, sell_price_ton, pnl,
        )
        return ExecutionResult(True, f"продано, чистыми {pnl:+.3f} TON", position_id)

    # --- Симуляция продажи ------------------------------------------------

    async def settle_paper_positions(self) -> list[int]:
        """Закрываем бумажные позиции по реальным рыночным сделкам.

        Позиция считается проданной, если после выставления в ленте прошла
        сделка по нашей коллекции и модели с ценой не ниже нашего аска.
        Это консервативно: очередь продавцов игнорируется, но зато нет
        фантазий о мгновенной продаже по любой цене.
        """
        closed: list[int] = []

        async with session_scope() as session:
            positions = list(
                (
                    await session.execute(
                        select(Position).where(
                            Position.status == "listed", Position.paper.is_(True)
                        )
                    )
                ).scalars()
            )

        for position in positions:
            if position.ask_price_ton is None:
                continue
            since = _aware(position.last_repriced_at or position.bought_at)

            async with session_scope() as session:
                query = select(SaleRecord).where(
                    SaleRecord.collection == position.collection,
                    SaleRecord.sold_at >= since,
                    SaleRecord.price_ton >= position.ask_price_ton,
                    SaleRecord.suspicious.is_(False),
                )
                if position.model:
                    query = query.where(SaleRecord.model == position.model)
                matched = (await session.execute(query.limit(1))).scalar()

            if matched is not None:
                result = await self.close(
                    position.id,
                    position.ask_price_ton,
                    "симуляция: рынок прошёл по цене аска",
                )
                if result.ok:
                    closed.append(position.id)

        return closed

    # --- Служебное -------------------------------------------------------

    def _fee_sell(self, market: str | None) -> float:
        cfg = self.settings.marketplaces.get(market or "")
        return cfg.fee_sell if cfg else 0.05

    def _fee_buy(self, market: str | None) -> float:
        cfg = self.settings.marketplaces.get(market or "")
        return cfg.fee_buy if cfg else 0.0

    async def _log(
        self,
        position_id: int | None,
        action: str,
        market: str,
        price: float | None,
        detail: str,
        *,
        ok: bool = True,
    ) -> None:
        async with session_scope() as session:
            session.add(
                TradeLog(
                    position_id=position_id,
                    action=action,
                    market=market,
                    price_ton=price,
                    detail=detail,
                    ok=ok,
                    paper=self.paper,
                )
            )


def _gift_from_snapshot(listing: ListingSnapshot):
    from app.analytics.signals import listing_to_gift

    return listing_to_gift(listing)


def _aware(value):
    if value is None:
        return utcnow() - timedelta(days=1)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def build_adapter_for(market: Market, settings: Settings) -> Marketplace:
    return registry.build_adapter(market, settings)
