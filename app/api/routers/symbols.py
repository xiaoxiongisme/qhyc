"""/symbols —— 品种主表 + **合约代码对照表**（contract_code_map）查询

代码表说明
----------
``contract_code_map`` 登记同一合约在**各源的原生写法**与**标准码**的对应关系：

* ``std_symbol``     标准码：品种大写 + YYMM 四位（``AP2701`` / ``CU2611``）——**库内唯一口径**
* ``official_symbol`` 交易所官方源码（郑商所 3 位 ``AP701``、上期小写 ``cu2611``）
* ``sina_symbol``    新浪源写法（全 4 位大写 ``AP2701``）
* ``tqsdk_symbol``   天勤订阅码（``CZCE.AP701``，郑商所仍 3 位）

换算函数见 ``app.core.symbol_code``。**下方 ``/contracts*`` 路由必须声明在
``/{symbol}`` 之前**，否则会被通配路由吞掉。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import symbol_code as SC
from app.core.db import fastapi_db_dep
from app.models import ContractCodeMap
from app.repositories.symbol_repo import SymbolRepository
from app.schemas.common import SymbolOut

router = APIRouter()


@router.get("/contracts")
def list_contract_codes(
    exchange: Optional[str] = None,
    product: Optional[str] = None,
    limit: int = 500,
    db: Session = Depends(fastapi_db_dep),
):
    """合约代码对照表（可按交易所/品种筛选）。"""
    stmt = select(ContractCodeMap).order_by(
        ContractCodeMap.exchange, ContractCodeMap.std_symbol
    )
    if exchange:
        stmt = stmt.where(ContractCodeMap.exchange == exchange.upper())
    if product:
        stmt = stmt.where(ContractCodeMap.product == SC.product_of(product) or product.upper())
    rows = db.execute(stmt.limit(max(1, min(limit, 5000)))).scalars().all()
    return {
        "count": len(rows),
        "rows": [
            {
                "exchange": r.exchange,
                "std_symbol": r.std_symbol,
                "product": r.product,
                "official_symbol": r.official_symbol,
                "sina_symbol": r.sina_symbol,
                "tqsdk_symbol": r.tqsdk_symbol,
                "deliv_year": r.deliv_year,
                "deliv_month": r.deliv_month,
                "name": r.name,
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in rows
        ],
    }


@router.get("/contracts/resolve")
def resolve_contract(
    code: str,
    exchange: Optional[str] = None,
    db: Session = Depends(fastapi_db_dep),
):
    """任意写法 → 标准码 + 各源写法（先查表，表内没有就现场换算）。"""
    std = SC.to_std(code, exchange=exchange)
    row = None
    if exchange or SC.is_std(std):
        stmt = select(ContractCodeMap).where(ContractCodeMap.std_symbol == std)
        if exchange:
            stmt = stmt.where(ContractCodeMap.exchange == exchange.upper())
        row = db.execute(stmt).scalars().first()
    if row is None:
        # 表内没有：退化为纯换算结果（含交易所时补出原生/天勤写法）
        ex = (exchange or "").upper()
        return {
            "input": code,
            "std_symbol": std,
            "exchange": ex or None,
            "official_symbol": SC.to_native(std, ex) if ex else None,
            "sina_symbol": SC.to_sina(std, ex or None),
            "tqsdk_symbol": SC.to_tqsdk(std, ex) if ex else None,
            "in_code_map": False,
        }
    return {
        "input": code,
        "std_symbol": row.std_symbol,
        "exchange": row.exchange,
        "product": row.product,
        "official_symbol": row.official_symbol,
        "sina_symbol": row.sina_symbol,
        "tqsdk_symbol": row.tqsdk_symbol,
        "deliv_year": row.deliv_year,
        "deliv_month": row.deliv_month,
        "name": row.name,
        "first_seen": row.first_seen.isoformat() if row.first_seen else None,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        "in_code_map": True,
    }


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