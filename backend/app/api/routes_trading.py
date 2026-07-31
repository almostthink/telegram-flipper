"""Торговля: позиции, покупка, управление движком, статистика, бэктест."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.analytics import backtest as backtest_mod
from app.analytics import stats as stats_mod
from app.api.deps import get_engine
from app.config import settings
from app.storage.db import session_scope
from app.storage.models import Position, TradeLog

router = APIRouter(tags=["trading"])


class BuyRequest(BaseModel):
    market: str
    listing_id: str


class SellRequest(BaseModel):
    price_ton: float


@router.get("/engine")
async def engine_status() -> dict:
    return get_engine().status()


@router.post("/engine/cycle")
async def run_cycle(deep: bool = Query(default=True)) -> dict:
    """Запустить полный цикл вручную."""
    report = await get_engine().run_cycle(deep=deep)
    return report.as_dict()


@router.post("/orders/run")
async def run_orders() -> dict:
    """Пересмотреть заявки прямо сейчас, не дожидаясь расписания."""
    report = await get_engine().run_orders()
    return report.as_dict()


@router.post("/engine/kill")
async def kill_switch() -> dict:
    engine = get_engine()
    engine.kill_switch()
    return engine.status()


@router.post("/engine/reset-breaker")
async def reset_breaker() -> dict:
    engine = get_engine()
    engine.risk.reset()
    return engine.status()


@router.get("/positions")
async def positions(
    status: str = Query(default="open", pattern="^(open|closed|all)$"),
    limit: int = Query(default=100, le=500),
) -> dict:
    async with session_scope() as session:
        query = (
            select(Position)
            .where(Position.paper.is_(settings.paper_mode))
            .order_by(Position.bought_at.desc())
            .limit(limit)
        )
        if status == "open":
            query = query.where(Position.status.in_(("open", "listed")))
        elif status == "closed":
            query = query.where(Position.status == "closed")

        rows = list((await session.execute(query)).scalars())

    return {"positions": [_position_dict(row) for row in rows], "paper": settings.paper_mode}


@router.post("/positions/buy")
async def buy(request: BuyRequest) -> dict:
    ok, detail = await get_engine().buy_manually(request.listing_id, request.market)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": detail}


@router.post("/positions/{position_id}/sell")
async def sell(position_id: int, request: SellRequest) -> dict:
    """Ручное закрытие позиции по указанной цене."""
    engine = get_engine()
    result = await engine.executor.close(position_id, request.price_ton, "закрыто вручную")
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.detail)

    async with session_scope() as session:
        position = await session.get(Position, position_id)
        collection = position.collection if position else ""
        pnl = position.net_pnl_ton if position else 0.0
    engine.risk.register_close(collection, pnl or 0.0)
    return {"ok": True, "detail": result.detail}


@router.post("/positions/{position_id}/freeze")
async def freeze(position_id: int, frozen: bool = Query(default=True)) -> dict:
    """Заморозка позиции: движок её не выставляет и не переоценивает.

    Так подарок забирают себе — чтобы перенести на другую площадку
    вручную. Пока позиция не заморожена, она продаётся там, где куплена.
    """
    ok, detail = await get_engine().set_frozen(position_id, frozen)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": detail}


@router.post("/positions/{position_id}/relist")
async def relist(position_id: int, request: SellRequest) -> dict:
    async with session_scope() as session:
        position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    engine = get_engine()
    if position.status == "open":
        result = await engine.executor.list_for_sale(position, request.price_ton)
    else:
        result = await engine.executor.reprice(position, request.price_ton, "изменено вручную")

    if not result.ok:
        raise HTTPException(status_code=400, detail=result.detail)
    return {"ok": True, "detail": result.detail}


@router.get("/journal")
async def journal(limit: int = Query(default=200, le=1000)) -> dict:
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(TradeLog).order_by(TradeLog.at.desc()).limit(limit)
                )
            ).scalars()
        )

    return {
        "entries": [
            {
                "id": row.id,
                "position_id": row.position_id,
                "at": row.at.isoformat(),
                "action": row.action,
                "market": row.market,
                "price_ton": row.price_ton,
                "detail": row.detail,
                "ok": row.ok,
                "paper": row.paper,
            }
            for row in rows
        ]
    }


@router.get("/stats")
async def trading_stats(days: int = Query(default=30, ge=1, le=365)) -> dict:
    result = await stats_mod.compute(paper=settings.paper_mode, days=days)
    return result.as_dict()


@router.post("/backtest")
async def run_backtest(days: int = Query(default=30, ge=1, le=180)) -> dict:
    result = await backtest_mod.run(settings, days=days)
    return result.as_dict()


def _position_dict(row: Position) -> dict:
    unrealized = None
    if row.status != "closed" and row.ask_price_ton:
        fee = settings.marketplaces.get(row.market)
        fee_sell = fee.fee_sell if fee else 0.05
        unrealized = row.ask_price_ton * (1 - fee_sell) - row.buy_price_ton

    return {
        "id": row.id,
        "market": row.market,
        "collection": row.collection,
        "model": row.model,
        "backdrop": row.backdrop,
        "symbol": row.symbol,
        "buy_price_ton": round(row.buy_price_ton, 3),
        "ask_price_ton": round(row.ask_price_ton, 3) if row.ask_price_ton else None,
        "sell_price_ton": round(row.sell_price_ton, 3) if row.sell_price_ton else None,
        "fair_value_at_buy": (
            round(row.fair_value_at_buy, 3) if row.fair_value_at_buy else None
        ),
        "expected_tts_hours": row.expected_tts_hours,
        "net_pnl_ton": round(row.net_pnl_ton, 3) if row.net_pnl_ton is not None else None,
        "unrealized_ton": round(unrealized, 3) if unrealized is not None else None,
        "reprice_count": row.reprice_count,
        "status": row.status,
        "bought_at": row.bought_at.isoformat(),
        "sold_at": row.sold_at.isoformat() if row.sold_at else None,
        "reason": row.reason,
        "paper": row.paper,
        "frozen": row.frozen,
    }
