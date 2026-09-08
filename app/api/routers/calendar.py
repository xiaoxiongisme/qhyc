"""/calendar - 交易日历（M1：官方日历未接入，支持推断模式 R2）"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.ingest.calendar_infer import calendar_mode, infer_calendar
from app.repositories.calendar_repo import CalendarRepository

router = APIRouter()


class InferRequest(BaseModel):
    min_ratio: float = Field(0.8, gt=0.1, le=1.0, description="当日有数据品种占比阈值")


@router.get("/mode")
def mode(db: Session = Depends(fastapi_db_dep)):
    """R2 日历模式：official / inferred / empty（看板据此显示"日历=推断模式"）"""
    return {"mode": calendar_mode(db)}


@router.post("/infer")
def infer(req: InferRequest, db: Session = Depends(fastapi_db_dep)):
    """R2：由品种间交叉（>=min_ratio 品种有数据）推断交易日历，写入 INF 分列"""
    return infer_calendar(db, min_ratio=req.min_ratio)


@router.get("/open")
def open_dates(
    exchange: str,
    start: date = Query(...),
    end: date = Query(...),
    db: Session = Depends(fastapi_db_dep),
):
    rows = CalendarRepository(db).open_dates(exchange, start, end)
    return {"exchange": exchange, "count": len(rows), "dates": [r.isoformat() for r in rows]}


@router.get("/missing")
def missing_dates(
    symbol: str,
    start: date = Query(...),
    end: date = Query(...),
    exchange: str = Query(...),
    db: Session = Depends(fastapi_db_dep),
):
    """交易日历 vs daily_bar 的差集（缺失检测）"""
    from sqlalchemy import select
    from app.models import DailyBar

    cal = CalendarRepository(db)
    open_dates = set(cal.open_dates(exchange, start, end))
    have = {
        r[0]
        for r in db.execute(
            select(DailyBar.trade_date).where(
                DailyBar.symbol == symbol,
                DailyBar.trade_date >= start,
                DailyBar.trade_date <= end,
            )
        ).all()
    }
    missing = sorted(open_dates - have)
    return {
        "symbol": symbol,
        "exchange": exchange,
        "missing_count": len(missing),
        "missing": [d.isoformat() for d in missing],
    }