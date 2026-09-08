from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class DailyBarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    settle: float | None = None
    volume: int | None = None
    amount: float | None = None
    oi: int | None = None
    ret_close: float | None = None
    ret_settle: float | None = None
    ret5: float | None = None
    ret20: float | None = None
    src: str


class MainContinuousOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product: str
    trade_date: date
    raw_open: float | None = None
    raw_high: float | None = None
    raw_low: float | None = None
    raw_close: float | None = None
    raw_volume: int | None = None
    raw_oi: int | None = None
    adj_open: float | None = None
    adj_high: float | None = None
    adj_low: float | None = None
    adj_close: float | None = None
    adj_volume: int | None = None
    adj_oi: int | None = None
    underlying: str | None = None
    change_flag: bool
    src: str