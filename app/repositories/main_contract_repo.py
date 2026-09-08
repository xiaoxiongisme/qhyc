"""主连换月映射仓储"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MainContractMap


class MainContractRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, rows: list[dict]) -> int:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        if not rows:
            return 0
        stmt = pg_insert(MainContractMap).values(rows)
        update_cols = {
            "main_symbol": stmt.excluded.main_symbol,
            "underlying": stmt.excluded.underlying,
            "change_flag": stmt.excluded.change_flag,
            "delta": stmt.excluded.delta,
            "src": stmt.excluded.src,
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "exchange", "product"],
            set_=update_cols,
        )
        self.session.execute(stmt)
        return len(rows)

    def query(
        self,
        product: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[MainContractMap]:
        stmt = select(MainContractMap)
        if product:
            stmt = stmt.where(MainContractMap.product == product)
        if start:
            stmt = stmt.where(MainContractMap.trade_date >= start)
        if end:
            stmt = stmt.where(MainContractMap.trade_date <= end)
        stmt = stmt.order_by(MainContractMap.trade_date.desc())
        return list(self.session.execute(stmt).scalars().all())