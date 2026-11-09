"""融合策略 推送留痕 / 持仓 查询 API

★ 2026-11-09（CL-20261109-18）：`GET /fusion/push_log`、`GET /fusion/signals` **已退役**，
   固定返回 410 Gone。理由：二者背后是 `fusion_push_log` 表，该表 2026-11-06 23:04 之后
   不再增长（生产推送源已于 11-08 唯一化为 V5X.3，V3.4 停止评估与推送），已被 DROP。
   历史 325 条已归档 `/home/ubuntu/qhyc_work/backup/fusion_push_log_20261109.csv`。
   要看「现在到底发了哪些信号」，请查 `v5x_signal_log`（结构化、含 notify_ok / 失败原因）。
   保留 410 而不是直接删路由：老书签/脚本能拿到明确原因，而不是含糊的 404。

- GET /fusion/push_log    [已退役 410] 推送历史（原文留痕，表已删除）
- GET /fusion/signals     [已退役 410] 只看「有信号」的那几轮（同上）
- GET /fusion/positions   当前持仓快照（含上次推送的止损位）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep, get_engine
from app.strategies.fusion_signal import (
    FusionPosition,
    ensure_fusion_table,
)

router = APIRouter()

_ENSURED = False

#: 410 的统一说明。写在一处，改一次就够。
_RETIRED_DETAIL = (
    "该接口已退役（CL-20261109-18）：底层表 fusion_push_log 已于 2026-11-09 DROP。"
    "生产推送源自 2026-11-08 起唯一化为 V5X.3，历史 325 条推送原文已归档到 "
    "/home/ubuntu/qhyc_work/backup/fusion_push_log_20261109.csv。"
    "查当前信号请用 v5x_signal_log（结构化，含 notify_ok / 失败原因）。"
)


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
def push_log_retired():
    """[已退役] 推送留痕。表已 DROP → 410。"""
    raise HTTPException(status_code=410, detail=_RETIRED_DETAIL)


@router.get("/fusion/signals")
def signals_retired():
    """[已退役] 只看含信号的推送。与 push_log 同源，一并 410。"""
    raise HTTPException(status_code=410, detail=_RETIRED_DETAIL)


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
