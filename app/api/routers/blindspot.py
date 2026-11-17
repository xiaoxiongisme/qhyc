"""少数面发现（L0 阅读线索，只读展示）—— blindspot schema 专用 API。

CL-20261116-01：数据+展示层搬入。硬边界：
  * 本路由**只读** blindspot schema，绝不写、绝不引用任何交易表
    （l0_raw/l1_mkt/l2_adj/l3_ref/public 交易数据/融合信号/预测/回测）。
  * 结论性声明固定随响应返回：L0 阅读线索，不可用于交易参考。
  * 由不变量 BS-SCHEMA-WIRED / BS-ISOLATED-FROM-TRADING 强制。

数据由本地 blindspot run.py 采集，经 research/blindspot_sync.py 幂等推送；
qhyc 侧不新增采集定时任务。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep

router = APIRouter()

DISCLAIMER = "L0 阅读线索，可用于阅读、不可用于交易参考；非交易信号"


@router.get("/meta")
def get_meta(db: Session = Depends(fastapi_db_dep)):
    """同步水位与口径元信息 + 固定免责声明。"""
    rows = db.execute(text(
        "SELECT key, value, updated_at FROM blindspot.sync_meta ORDER BY key"
    )).mappings().all()
    counts = dict(db.execute(text(
        "SELECT 'items',count(*) FROM blindspot.items UNION ALL "
        "SELECT 'quotes',count(*) FROM blindspot.quotes UNION ALL "
        "SELECT 'signals',count(*) FROM blindspot.signals UNION ALL "
        "SELECT 'pit_snapshots',count(*) FROM blindspot.pit_snapshots"
    )).all())
    return {
        "disclaimer": DISCLAIMER,
        "meta": {r["key"]: {"value": r["value"], "updated_at": str(r["updated_at"])} for r in rows},
        "counts": counts,
    }


@router.get("/snapshot")
def get_snapshot(db: Session = Depends(fastapi_db_dep)):
    """最新 PIT 快照（数据截止时刻、口径指纹、degraded 状态）。"""
    row = db.execute(text(
        "SELECT asof, config_hash, captured_at, n_signals, n_quotes, n_priced, "
        "degraded, degrade_reasons, data_cutoff, signal_ready_at "
        "FROM blindspot.v_latest_snapshot"
    )).mappings().first()
    return {"disclaimer": DISCLAIMER, "snapshot": dict(row) if row else None}


@router.get("/signals")
def get_signals(asof: str | None = None, limit: int = 100,
                db: Session = Depends(fastapi_db_dep)):
    """少数面信号榜（按分数降序）。无方向、非交易信号，仅展示。"""
    if asof:
        sql = text(
            "SELECT asof, variety, stype, score, evidence FROM blindspot.signals "
            "WHERE asof = :asof ORDER BY score DESC, variety, stype LIMIT :lim"
        )
        rows = db.execute(sql, {"asof": asof, "lim": limit}).mappings().all()
    else:
        rows = db.execute(text(
            "SELECT asof, variety, stype, score, evidence FROM blindspot.v_signal_board LIMIT :lim"
        ), {"lim": limit}).mappings().all()
    return {"disclaimer": DISCLAIMER, "signals": [dict(r) for r in rows]}


@router.get("/items")
def get_items(kind: str | None = None, limit: int = 100,
              db: Session = Depends(fastapi_db_dep)):
    """采集条目（资讯/公告/通知/长尾帖），含原文链接与品种映射；仅展示。"""
    if kind:
        rows = db.execute(text(
            "SELECT id, source, kind, title, url, published, varieties, attention "
            "FROM blindspot.items WHERE kind = :kind "
            "ORDER BY published DESC NULLS LAST, id DESC LIMIT :lim"
        ), {"kind": kind, "lim": limit}).mappings().all()
    else:
        rows = db.execute(text(
            "SELECT id, source, kind, title, url, published, varieties, attention "
            "FROM blindspot.items ORDER BY published DESC NULLS LAST, id DESC LIMIT :lim"
        ), {"lim": limit}).mappings().all()
    return {"disclaimer": DISCLAIMER, "items": [dict(r) for r in rows]}


@router.get("/quotes")
def get_quotes(asof: str | None = None, limit: int = 100,
               db: Session = Depends(fastapi_db_dep)):
    """行情快照（涨幅/成交/持仓），用于对照关注度，非交易信号。"""
    if asof:
        rows = db.execute(text(
            "SELECT asof, code, name, variety, chg, vol, amount, oi, oi_chg, snapshot_at "
            "FROM blindspot.quotes WHERE asof = :asof "
            "ORDER BY abs(chg) DESC NULLS LAST LIMIT :lim"
        ), {"asof": asof, "lim": limit}).mappings().all()
    else:
        rows = db.execute(text(
            "SELECT asof, code, name, variety, chg, vol, amount, oi, oi_chg, snapshot_at "
            "FROM blindspot.quotes WHERE asof = (SELECT MAX(asof) FROM blindspot.quotes) "
            "ORDER BY abs(chg) DESC NULLS LAST LIMIT :lim"
        ), {"lim": limit}).mappings().all()
    return {"disclaimer": DISCLAIMER, "quotes": [dict(r) for r in rows]}
