"""品种元数据仓储"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FuturesSymbol


class SymbolRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_all(self, only_active: bool = True) -> list[FuturesSymbol]:
        stmt = select(FuturesSymbol).order_by(FuturesSymbol.symbol)
        if only_active:
            stmt = stmt.where(FuturesSymbol.active.is_(True))
        return list(self.session.execute(stmt).scalars().all())

    def list_mains(self) -> list[FuturesSymbol]:
        stmt = (
            select(FuturesSymbol)
            .where(FuturesSymbol.is_main.is_(True))
            .order_by(FuturesSymbol.symbol)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get(self, symbol: str) -> FuturesSymbol | None:
        return self.session.get(FuturesSymbol, symbol)