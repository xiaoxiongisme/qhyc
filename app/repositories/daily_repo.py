"""日线仓储"""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import DailyBar


class DailyBarRepository:
    def __init__(self, session: Session):
        self.session = session

    def query(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        limit: int = 1000,
    ) -> list[DailyBar]:
        stmt = select(DailyBar).where(DailyBar.symbol == symbol)
        if start:
            stmt = stmt.where(DailyBar.trade_date >= start)
        if end:
            stmt = stmt.where(DailyBar.trade_date <= end)
        stmt = stmt.order_by(DailyBar.trade_date.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def list_symbols_with_data(self) -> list[str]:
        rows = self.session.execute(
            select(DailyBar.symbol).distinct().order_by(DailyBar.symbol)
        ).all()
        return [r[0] for r in rows]

    def max_trade_date(self, symbol: str) -> date | None:
        row = self.session.execute(
            select(DailyBar.trade_date)
            .where(DailyBar.symbol == symbol)
            .order_by(DailyBar.trade_date.desc())
            .limit(1)
        ).first()
        return row[0] if row else None

    def count(self, symbol: str | None = None) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(DailyBar)
        if symbol:
            stmt = stmt.where(DailyBar.symbol == symbol)
        return int(self.session.execute(stmt).scalar() or 0)