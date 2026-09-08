"""/bars/{symbol}"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.repositories.daily_repo import DailyBarRepository
from app.repositories.main_continuous_repo import MainContinuousRepository
from app.schemas.bar import DailyBarOut, MainContinuousOut

router = APIRouter()


@router.get("/{symbol}", response_model=list[DailyBarOut])
def get_daily_bars(
    symbol: str,
    freq: str = Query("daily", pattern="^(daily|hourly)$"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(500, le=5000),
    db: Session = Depends(fastapi_db_dep),
):
    if freq != "daily":
        from fastapi import HTTPException

        raise HTTPException(501, f"freq={freq} not implemented in M1")
    rows = DailyBarRepository(db).query(symbol, start, end, limit=limit)
    return [_to_out(r) for r in rows]


@router.get("/main/{product}", response_model=list[MainContinuousOut])
def get_main_continuous(
    product: str,
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(500, le=5000),
    db: Session = Depends(fastapi_db_dep),
):
    rows = MainContinuousRepository(db).query(product, start, end, limit=limit)
    return [_to_mc_out(r) for r in rows]


def _to_out(r) -> DailyBarOut:
    return DailyBarOut(
        symbol=r.symbol,
        trade_date=r.trade_date,
        open=float(r.open) if r.open is not None else None,
        high=float(r.high) if r.high is not None else None,
        low=float(r.low) if r.low is not None else None,
        close=float(r.close) if r.close is not None else None,
        settle=float(r.settle) if r.settle is not None else None,
        volume=r.volume,
        amount=float(r.amount) if r.amount is not None else None,
        oi=r.oi,
        ret_close=float(r.ret_close) if r.ret_close is not None else None,
        ret_settle=float(r.ret_settle) if r.ret_settle is not None else None,
        ret5=float(r.ret5) if r.ret5 is not None else None,
        ret20=float(r.ret20) if r.ret20 is not None else None,
        src=r.src,
    )


def _to_mc_out(r) -> MainContinuousOut:
    return MainContinuousOut(
        product=r.product,
        trade_date=r.trade_date,
        raw_open=float(r.raw_open) if r.raw_open is not None else None,
        raw_high=float(r.raw_high) if r.raw_high is not None else None,
        raw_low=float(r.raw_low) if r.raw_low is not None else None,
        raw_close=float(r.raw_close) if r.raw_close is not None else None,
        raw_volume=r.raw_volume,
        raw_oi=r.raw_oi,
        adj_open=float(r.adj_open) if r.adj_open is not None else None,
        adj_high=float(r.adj_high) if r.adj_high is not None else None,
        adj_low=float(r.adj_low) if r.adj_low is not None else None,
        adj_close=float(r.adj_close) if r.adj_close is not None else None,
        adj_volume=r.adj_volume,
        adj_oi=r.adj_oi,
        underlying=r.underlying,
        change_flag=r.change_flag,
        src=r.src,
    )