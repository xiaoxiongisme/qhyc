"""/symbols"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.repositories.symbol_repo import SymbolRepository
from app.schemas.common import SymbolOut

router = APIRouter()


@router.get("", response_model=list[SymbolOut])
def list_symbols(
    only_main: bool = False,
    only_active: bool = True,
    db: Session = Depends(fastapi_db_dep),
):
    repo = SymbolRepository(db)
    if only_main:
        rows = repo.list_mains()
    else:
        rows = repo.list_all(only_active=only_active)
    return [
        SymbolOut(
            symbol=r.symbol,
            name=r.name,
            exchange=r.exchange,
            unit=r.unit,
            multiplier=float(r.multiplier) if r.multiplier is not None else None,
            product=r.product,
            is_main=r.is_main,
            main_symbol=r.main_symbol,
            active=r.active,
        )
        for r in rows
    ]


@router.get("/{symbol}", response_model=SymbolOut)
def get_symbol(symbol: str, db: Session = Depends(fastapi_db_dep)):
    r = SymbolRepository(db).get(symbol)
    if not r:
        from fastapi import HTTPException

        raise HTTPException(404, f"symbol {symbol} not found")
    return SymbolOut(
        symbol=r.symbol,
        name=r.name,
        exchange=r.exchange,
        unit=r.unit,
        multiplier=float(r.multiplier) if r.multiplier is not None else None,
        product=r.product,
        is_main=r.is_main,
        main_symbol=r.main_symbol,
        active=r.active,
    )