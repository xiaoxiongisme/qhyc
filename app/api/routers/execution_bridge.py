# -*- coding: utf-8 -*-
"""P0-2 · 执行桥接 REST —— 供**无限易 PythonGO 侧 QhycBridge** 取单与回报。

背景
----
期货公司（广发）的合规要求：程序化须走**已报备的桌面交易软件**（本方案选用无限易，
免费且无服务费）。因此 qhyc **不再自己连 CTP 柜台**，改为：

    qhyc 策略/反解层/持仓管理/止损（全部保留，一行不改）
        ↓  execution_order 表，status=NEW
    【本模块】对外暴露 pending / claim / report 三个接口
        ↓
    无限易 PythonGO「QhycBridge」轮询取单 → 调用无限易下单 API → 回报写回

三个接口
--------
* ``GET  /execution/bridge/pending``  列出待执行订单（只读，**不认领**）
* ``POST /execution/bridge/claim``    **原子认领**：``UPDATE ... WHERE status='NEW' RETURNING``
* ``POST /execution/bridge/report``   成交/状态回报回写

为什么必须先 claim 再下单
--------------------------
若无限易侧只 GET pending 就下单，两次轮询会拿到同一笔 → **重复下单**。
claim 用单条 SQL 的条件更新保证**只有一个认领者**（rows affected = 1 才算成功），
这是防重复成交的最后一道闸。

为什么不在无限易侧直接改 status
-------------------------------
状态机与幂等规则集中在 qhyc（``persistence.update_status`` 有非法迁移校验），
保持单一事实来源；本模块只是薄薄一层 REST 入口。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

# 注意：鉴权依赖由 app/api/__init__.py 注册时统一注入（这里是 from app.api import _auth
# 会形成循环导入），本文件内不重复声明。
from app.core.db import session_scope

router = APIRouter()

#: 认领后的目标状态（与 execution_runtime.submit_order 一致）
_CLAIMED_STATUS = "SENT"


class ClaimReq(BaseModel):
    order_id: int = Field(..., gt=0, description="qhyc 订单内部 id（execution_order.id）")
    client_token: Optional[str] = Field(
        None, description="认领方标识，便于事后追溯是谁把这单拿走了")


class ReportReq(BaseModel):
    order_id: int = Field(..., gt=0)
    status: str = Field(..., description="SENT / PARTIAL / FILLED / REJECTED / CANCELED")
    broker_order_id: Optional[str] = None
    filled_lots: int = 0
    avg_price: float = 0.0
    error: Optional[str] = None


# ------------------------------------------------------------------ pending
@router.get("/bridge/pending", tags=["execution-bridge"])
def bridge_pending(limit: int = 20) -> dict:
    """列出待执行订单（status=NEW）。**只读，不认领**。"""
    limit = max(1, min(int(limit), 200))
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT id, real_symbol, exchange,
                       direction, action, price, lots,
                       multiplier, notional, channel, account
                  FROM execution_order
                 WHERE status = 'NEW'
                 ORDER BY id
                 LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    return {
        "count": len(rows),
        "orders": [
            {
                "id": int(r["id"]),
                "real_symbol": r["real_symbol"],
                "exchange": r["exchange"],
                "direction": r["direction"],
                "action": r["action"],
                "price": _f(r["price"]),
                "lots": int(r["lots"] or 0),
                "multiplier": _f(r["multiplier"]),
                "notional": _f(r["notional"]),
                "tick": 0.0,   # execution_order 无 tick 列；如需请从 dim_variety 关联
                "channel": r["channel"],
                "account": r["account"],
            }
            for r in rows
        ],
    }


# ------------------------------------------------------------------ claim
@router.post("/bridge/claim", tags=["execution-bridge"])
def bridge_claim(req: ClaimReq) -> dict:
    """原子认领一笔 NEW 订单。

    返回 ``claimed=True`` 的调用方**才允许下单**；
    ``claimed=False`` 表示这单已被别人（另一个网关实例 / 另一台终端）抢走或被 abiotic
    改变状态 → **必须放弃**，否则就是重复下单。
    """
    with session_scope() as s:
        row = s.execute(
            text(
                """
                UPDATE execution_order
                   SET status = :dst,
                       updated_at = now()
                 WHERE id = :id
                   AND status = 'NEW'
                RETURNING id, real_symbol, exchange, direction, action,
                          price, lots, multiplier
                """
            ),
            {"id": req.order_id, "dst": _CLAIMED_STATUS},
        ).mappings().first()

        if row is None:
            # 抢不到：查一下当前状态，给调用方一个可诊断的理由
            cur = s.execute(
                text("SELECT status FROM execution_order WHERE id = :id"),
                {"id": req.order_id},
            ).scalar()
            if cur is None:
                raise HTTPException(status_code=404, detail=f"订单不存在：{req.order_id}")
            return {
                "claimed": False,
                "order_id": req.order_id,
                "reason": f"订单当前状态 {cur}，不是 NEW（已被认领或已终结）",
            }

        rec = dict(row)
    return {
        "claimed": True,
        "order_id": int(rec["id"]),
        "order": {
            "id": int(rec["id"]),
            "real_symbol": rec["real_symbol"],
            "exchange": rec["exchange"],
            "direction": rec["direction"],
            "action": rec["action"],
            "price": _f(rec["price"]),
            "lots": int(rec["lots"] or 0),
            "multiplier": _f(rec["multiplier"]),
        },
    }


# ------------------------------------------------------------------ report
@router.post("/bridge/report", tags=["execution-bridge"])
def bridge_report(req: ReportReq) -> dict:
    """无限易侧回报成交/拒单/撤单结果。

    状态迁移仍走 ``persistence.update_status`` 的校验（非法迁移会被拦住并 400），
    成交明细写 ``execution_fill``，持仓由 ``upsert_position`` 维护。
    """
    # 延迟导入：避免在中途依赖循环（persistence 用 app.core.db）
    from app.execution import persistence

    order = persistence.get_order(req.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单不存在：{req.order_id}")

    status = (req.status or "").upper()
    # DRYRUN 只留痕，不改状态（无限易侧演练时用）
    if status == "DRYRUN":
        return {"ok": True, "order_id": req.order_id, "action": "logged_only",
                "note": "dryRun 回报，未改动订单状态"}

    try:
        persistence.update_status(
            req.order_id,
            status,
            broker_order_id=req.broker_order_id,
            error=req.error,
        )
    except persistence.ExecutionStateError as e:
        # 非法迁移：多半是晚到的旧回报，按幂等处理而非报错（避免刷屏 500）
        return {"ok": False, "order_id": req.order_id, "action": "skipped",
                "reason": f"状态迁移被拒（幂等）：{e}"}

    fill_warning = None
    if status in ("PARTIAL", "FILLED") and req.filled_lots > 0:
        # save_fill 要求显式 broker_fill_id（UNIQUE(order_id, broker_fill_id) 做幂等）；
        # 无限易侧若未提供成交号，用 broker_order_id + 手数+均价合成，保证重复推送不重复记账。
        fid = req.broker_order_id or "unknown"
        broker_fill_id = "{0}#{1}@{2}".format(fid, req.filled_lots, req.avg_price)
        from datetime import datetime, timezone

        try:
            persistence.save_fill(
                order_id=req.order_id,
                broker_fill_id=broker_fill_id,
                fill_ts=datetime.now(timezone.utc),
                fill_price=Decimal(str(req.avg_price)),
                fill_lots=req.filled_lots,
            )
        except Exception as e:
            fill_warning = "成交明细未落 execution_fill：{0!r}".format(e)
            return {"ok": False, "order_id": req.order_id, "action": "status_only",
                    "warning": fill_warning}

    return {"ok": True, "order_id": req.order_id, "action": "updated", "status": status,
            "fill_warning": fill_warning}


# ------------------------------------------------------------------ health
@router.get("/bridge/health", tags=["execution-bridge"])
def bridge_health() -> dict:
    with session_scope() as s:
        pending = s.execute(
            text("SELECT count(*) FROM execution_order WHERE status = 'NEW'")
        ).scalar() or 0
        open_rows = s.execute(
            text("SELECT count(*) FROM execution_order WHERE status IN ('SENT','PARTIAL')")
        ).scalar() or 0
    return {
        "channel": "INFINITRADER",
        "healthy": True,
        "pending": int(pending),
        "in_flight": int(open_rows),
        "note": "qhyc 侧桥接就绪；下一环是无限易 PythonGO 侧 QhycBridge 轮询",
    }


def _f(v: Any) -> Optional[float]:
    """Decimal/None → float，便于 JSON 序列化。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
