"""主连平滑/原始仓储"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MainContinuous


class MainContinuousRepository:
    def __init__(self, session: Session):
        self.session = session

    def query(
        self,
        product: str,
        start: date | None = None,
        end: date | None = None,
        limit: int = 1000,
    ) -> list[MainContinuous]:
        stmt = select(MainContinuous).where(MainContinuous.product == product)
        if start:
            stmt = stmt.where(MainContinuous.trade_date >= start)
        if end:
            stmt = stmt.where(MainContinuous.trade_date <= end)
        stmt = stmt.order_by(MainContinuous.trade_date.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def max_date(self, product: str) -> date | None:
        row = self.session.execute(
            select(MainContinuous.trade_date)
            .where(MainContinuous.product == product)
            .order_by(MainContinuous.trade_date.desc())
            .limit(1)
        ).first()
        return row[0] if row else None