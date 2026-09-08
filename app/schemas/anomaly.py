from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class AnomalyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    trade_date: date
    field: str
    akshare_val: float | None = None
    tqsdk_val: float | None = None
    diff: float | None = None
    threshold: float | None = None
    status: str
    note: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None