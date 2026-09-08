"""交易日历仓储"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TradeCalendar


class CalendarRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, rows: list[dict]) -> int:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        if not rows:
            return 0
        stmt = pg_insert(TradeCalendar).values(rows)
        update_cols = {
            "is_open": stmt.excluded.is_open,
            "sessions": stmt.excluded.sessions,
            "note": stmt.excluded.note,
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["exchange", "trade_date"],
            set_=update_cols,
        )
        self.session.execute(stmt)
        return len(rows)

    def is_open(self, exchange: str, trade_date: date) -> bool | None:
        row = self.session.execute(
            select(TradeCalendar.is_open).where(
                TradeCalendar.exchange == exchange,
                TradeCalendar.trade_date == trade_date,
            )
        ).first()
        return row[0] if row else None

    def open_dates(
        self, exchange: str, start: date, end: date
    ) -> list[date]:
        rows = self.session.execute(
            select(TradeCalendar.trade_date)
            .where(
                TradeCalendar.exchange == exchange,
                TradeCalendar.is_open.is_(True),
                TradeCalendar.trade_date >= start,
                TradeCalendar.trade_date <= end,
            )
            .order_by(TradeCalendar.trade_date)
        ).all()
        return [r[0] for r in rows]