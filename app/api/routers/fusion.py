"""融合策略 推送留痕 / 持仓 查询 API

「怕错过信号」的兜底：每条推送到手机的记录都会落 `fusion_push_log`，
无论消息被微信折叠、没点开、还是半夜静默，都能在这里按时间回查。

- GET /fusion/push_log    推送历史（倒序，可按 kind 过滤）
- GET /fusion/signals     只看「有信号」的那几轮（开/平/反手）
- GET /fusion/positions   当前持仓快照（含上次推送的止损位）
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep, get_engine
from app.strategies.fusion_signal import (
    FusionPosition,
    FusionPushLog,
    ensure_fusion_table,
)

router = APIRouter()

_ENSURED = False


def _ensure_once(db: Session) -> None:
    """首次访问时幂等建表（API 可能先于 scheduler 启动）。"""
    global _ENSURED
    if _ENSURED:
        return
    try:
        ensure_fusion_table(get_engine())
        _ENSURED = True
    except Exception:  # noqa: BLE001
        db.rollback()


@router.get("/fusion/push_log")
def push_log(
    limit: int = Query(50, ge=1, le=500),
    kind: Optional[str] = Query(None, description="signal / presession / heartbeat / startup"),
    with_content: bool = Query(True, description="是否返回完整正文"),
    db: Session = Depends(fastapi_db_dep),
):
    """推送留痕（按时间倒序）。"""
    _ensure_once(db)
    stmt = select(FusionPushLog).order_by(desc(FusionPushLog.id)).limit(limit)
    if kind:
        stmt = stmt.where(FusionPushLog.kind == kind)
    rows = db.execute(stmt).scalars().all()
    return {
        "count": len(rows),
        "rows": [
            {
                "id": r.id,
                "pushed_at": r.pushed_at.isoformat() if r.pushed_at else None,
                "kind": r.kind,
                "title": r.title,
                "n_signals": r.n_signals,
                "n_rows": r.n_rows,
                "delivered": bool(r.delivered),
                "via": r.via,
                **({"content": r.content} if with_content else {}),
            }
            for r in rows
        ],
    }


@router.get("/fusion/signals")
def signals(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(fastapi_db_dep),
):
    """只看「含开仓/平仓/反手信号」的推送（kinds 里的 signal）。"""
    return push_log(limit=limit, kind="signal", with_content=True, db=db)


@router.get("/fusion/positions")
def positions(db: Session = Depends(fastapi_db_dep)):
    """当前持仓快照（含引擎真实入场价、上次推送给你的止损位、开仓信号时间）。"""
    _ensure_once(db)
    rows = db.execute(select(FusionPosition).order_by(FusionPosition.symbol)).scalars().all()
    return {
        "count": len(rows),
        "nonflat": sum(1 for r in rows if r.position != "FLAT"),
        "rows": [
            {
                "symbol": r.symbol,
                "position": r.position,
                "entry_price": float(r.entry_price) if r.entry_price is not None else None,
                "entry_at": r.entry_at.isoformat() if r.entry_at else None,
                "last_pushed_stop": float(r.last_stop) if r.last_stop is not None else None,
                "be_done": bool(r.last_be_done),
                "signal_at": r.signal_at.isoformat() if r.signal_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }
