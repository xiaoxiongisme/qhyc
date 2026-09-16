"""§18.4/§18.5（v1.3.2）M6a：持仓/库存/基差 查询 API + 手动采集"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.models import Inventory, MemberPositionRank, SpotBasis

router = APIRouter()


@router.get("/positions/latest")
def positions_latest(
    exchange: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(fastapi_db_dep),
):
    """最新交易日的前 N 名会员持仓（按交易所+品种筛选）"""
    from sqlalchemy import desc

    sub = (
        select(MemberPositionRank.trade_date, func.max(MemberPositionRank.created_at).label("mx"))
        .group_by(MemberPositionRank.trade_date)
        .order_by(desc(MemberPositionRank.trade_date))
        .limit(1)
    )
    latest = db.execute(sub).first()
    if not latest:
        return {"trade_date": None, "rows": []}
    td = latest[0]
    stmt = (
        select(MemberPositionRank)
        .where(MemberPositionRank.trade_date == td)
        .order_by(MemberPositionRank.exchange, MemberPositionRank.symbol, MemberPositionRank.rank)
        .limit(limit)
    )
    if exchange:
        stmt = stmt.where(MemberPositionRank.exchange == exchange.upper())
    if symbol:
        stmt = stmt.where(MemberPositionRank.symbol == symbol.upper())
    rows = db.execute(stmt).scalars().all()
    return {
        "trade_date": td.isoformat(),
        "rows": [
            {
                "exchange": r.exchange,
                "symbol": r.symbol,
                "member": r.member,
                "rank": r.rank,
                "long_pos": r.long_pos,
                "short_pos": r.short_pos,
                "long_chg": r.long_chg,
                "short_chg": r.short_chg,
                "vol_pos": r.vol_pos,
            }
            for r in rows
        ],
    }


@router.get("/inventory/latest")
def inventory_latest(
    exchange: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(fastapi_db_dep),
):
    """最新库存（按品种聚合）"""
    stmt = (
        select(
            Inventory.report_date,
            Inventory.exchange,
            Inventory.symbol,
            func.sum(Inventory.inventory_qty).label("qty"),
            func.sum(Inventory.change_qty).label("chg"),
        )
        .group_by(Inventory.report_date, Inventory.exchange, Inventory.symbol)
        .order_by(Inventory.report_date.desc(), Inventory.symbol)
        .limit(limit)
    )
    if exchange:
        stmt = stmt.where(Inventory.exchange == exchange.upper())
    if symbol:
        stmt = stmt.where(Inventory.symbol == symbol.upper())
    rows = db.execute(stmt).all()
    return {
        "rows": [
            {
                "report_date": r[0].isoformat(),
                "exchange": r[1],
                "symbol": r[2],
                "inventory_qty": int(r[3] or 0),
                "change_qty": int(r[4] or 0),
            }
            for r in rows
        ]
    }


@router.post("/collect/{date_str}")
def collect_collectibles(
    date_str: str,
    include_positions: bool = True,
    include_inventory: bool = True,
    include_basis: bool = True,
    include_contract_bars: bool = False,
    db: Session = Depends(fastapi_db_dep),
):
    """手动触发一次采集（持仓 + 库存 + 基差 + 合约日线）；§18.13 矩阵驱动

    使用场景：scheduler 之外手工触发（盘后补采/历史回填）；不入库 task_run（轻量操作）
    """
    from app.ingest.inventory import collect_inventory_em, PRODUCT_TO_EM_SYM
    from app.ingest.member_position import collect_member_position
    from app.ingest.spot_basis import collect_spot_basis

    td = date.fromisoformat(date_str)
    out = {}
    if include_positions:
        out["positions"] = collect_member_position(db, td)
    if include_inventory:
        out["inventory"] = collect_inventory_em(db, list(PRODUCT_TO_EM_SYM.keys()))
    if include_basis:
        out["basis"] = collect_spot_basis(db, td, td)
    if include_contract_bars:
        from app.ingest.contract_bars import collect_contract_bars

        out["contract_bars"] = collect_contract_bars(db)
    return out


@router.get("/carry/latest")
def carry_latest(limit: int = 60, db: Session = Depends(fastapi_db_dep)):
    """§18.6 carry/期限结构因子（每品种最新）"""
    from app.ingest.contract_bars import carry_factors
    import pandas as pd

    rows = db.execute(
        text(
            """
            SELECT product, symbol, trade_date, close, oi
            FROM contract_daily
            WHERE trade_date = (SELECT max(trade_date) FROM contract_daily)
            ORDER BY product, oi DESC
            """
        )
    ).all()
    df = pd.DataFrame(rows, columns=["product", "symbol", "trade_date", "close", "oi"])
    out = []
    if not df.empty:
        for product, g in df.groupby("product"):
            f = carry_factors(g)
            if f:
                out.append({"product": product, **f})
    return {"rows": out}


@router.get("/basis/latest")
def basis_latest(
    symbol: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(fastapi_db_dep),
):
    """最新基差（按品种）"""
    stmt = select(SpotBasis).order_by(SpotBasis.report_date.desc(), SpotBasis.symbol).limit(limit)
    if symbol:
        stmt = stmt.where(SpotBasis.symbol == symbol.upper())
    rows = db.execute(stmt).scalars().all()
    return {
        "rows": [
            {
                "report_date": r.report_date.isoformat(),
                "symbol": r.symbol,
                "spot_price": float(r.spot_price) if r.spot_price is not None else None,
                "dominant_contract": r.dominant_contract,
                "dom_basis": float(r.dom_basis) if r.dom_basis is not None else None,
                "dom_basis_rate": float(r.dom_basis_rate) if r.dom_basis_rate is not None else None,
                "near_basis_rate": float(r.near_basis_rate) if r.near_basis_rate is not None else None,
            }
            for r in rows
        ]
    }
