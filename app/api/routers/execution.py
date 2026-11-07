# -*- coding: utf-8 -*-
"""执行层 REST 端点。

P0-1（反解）：`POST /execution/reverse` —— 信号 → 真实合约订单建议（**不直接下单**）。
P0-2 Sprint1（通道，DDL 030）：`/orders` `/positions` `/health` —— 订单落库/下发、持仓、通道健康。

鉴权沿用全局 _auth（见 app/api/__init__.py）。

⚠ **实盘门禁**：会**写库/下单**的端点受 `EXECUTION_ENABLED` 开关约束——关闭时返回 503
并明确说明，**不静默接受**（"开关看似生效实则照跑"是本项目重点缺陷类型）。
只读端点（health/positions/orders 列表）不受门禁，便于观测。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import fastapi_db_dep, session_scope
from app.execution import resolve_signal_to_order
from app.execution.schemas import ExecutionOrder, ReverseRequest

router = APIRouter()


def _require_enabled() -> None:
    """实盘门禁：EXECUTION_ENABLED != 1 → 503（fail-loud，不静默下单）。"""
    if not get_settings().execution.enabled:
        raise HTTPException(
            503,
            "EXECUTION_ENABLED != 1，实盘通道关闭中，拒绝写单/下单。"
            "（仿真验证请显式设置 EXECUTION_BROKER=sim 并开启 EXECUTION_ENABLED）",
        )


@router.post("/reverse", response_model=ExecutionOrder, tags=["execution"])
def reverse(req: ReverseRequest, db: Session = Depends(fastapi_db_dep)) -> ExecutionOrder:
    """信号(连续888/复权) → 真实合约订单建议。

    返回 ExecutionOrder；blocking_reasons 非空表示该单禁止下发（详见 PRD §3.4）。
    """
    return resolve_signal_to_order(db, req)


# --------------------------------------------------------------------------
# P0-2 Sprint1：通道（DDL 030）
# --------------------------------------------------------------------------
class SubmitOrderReq(BaseModel):
    """落单并（可选）下发。idempotency_key 缺省由 signal/real/action/日期推导。"""

    real_symbol: str = Field(..., examples=["RB2701"])
    exchange: str = Field(..., examples=["SHFE"])
    action: str = Field(..., pattern="^(OPEN|CLOSE|CLOSE_TODAY|CLOSE_YEST)$")
    direction: str = Field(..., pattern="^(BUY|SELL)$")
    price: float = Field(..., gt=0)
    lots: int = Field(..., gt=0)
    symbol_continuous: Optional[str] = None
    signal_id: Optional[str] = None
    multiplier: Optional[float] = None
    price_space: str = Field("raw", pattern="^(raw|adj)$")
    account: Optional[str] = None
    channel: Optional[str] = None
    dry_run: bool = Field(False, description="仅落库不下发（默认 False）")


@router.post("/orders", tags=["execution"])
def submit_order(req: SubmitOrderReq) -> dict:
    """落订单（幂等）并可选下发到网关。

    幂等：同 idempotency_key 重复调用返回**同一 order id**，绝不产生第二单。

    ★ 2026-11-06 新增**第二道幂等闸**（跨天场景）
      ----------------------------------------------------
      幂等键含**交易日**（`{signal_id}:{real_symbol}:{action}:{yyyymmdd}`），
      故同一 signal_id **跨天**不再幂等 —— 而"同一个信号"跨天仍只应有一单
      （例：止损单 signal_id 由品种+价格生成，同价跨天会再次平仓）。
      现：调用方**显式**给了 ``signal_id`` 时，下单前按
      ``(account, real_symbol, signal_id)`` 查一次**未终态**订单；命中即
      **拒新单**并返回既有单（只查不改，绝不改动既有订单）。

      ⚠ 仅对**显式** signal_id 生效：缺省 signal_id 是自动生成的
        ``MANUAL:{real_symbol}``，那不是"调用方声明的同一个信号"，
        若也拦跨天，运维第二天想再下一单会被误拒。
    """
    from datetime import date

    from app.execution import execution_runtime, persistence

    _require_enabled()
    cfg = get_settings().execution
    account = req.account if req.account is not None else cfg.account
    channel = req.channel or cfg.channel
    key = persistence.build_idempotency_key(
        req.signal_id or f"MANUAL:{req.real_symbol}", req.real_symbol, req.action, date.today())
    if req.signal_id:
        dup = persistence.find_order_by_signal(
            account, req.real_symbol, req.signal_id, only_unsettled=True)
        if dup is not None:
            return {
                "order_id": dup["id"],
                "idempotency_key": dup["idempotency_key"],
                "state": "DUPLICATE_BLOCKED",
                "reason": (
                    f"同 (account={account}, real_symbol={req.real_symbol}, "
                    f"signal_id={req.signal_id}) 已有未终态订单 id={dup['id']} "
                    f"status={dup['status']}（幂等键含交易日，跨天不再幂等）"
                    f"→ 本次**未落新单**（既有订单未被修改）"),
            }
    order = {
        "signal_id": req.signal_id or f"MANUAL:{req.real_symbol}",
        "symbol_continuous": req.symbol_continuous or f"{req.real_symbol[:-4]}888",
        "real_symbol": req.real_symbol,
        "exchange": req.exchange,
        "action": req.action,
        "direction": req.direction,
        "price": req.price,
        "price_space": req.price_space,
        "lots": req.lots,
        "multiplier": req.multiplier,
        "notional": (req.price * req.lots * req.multiplier) if req.multiplier else None,
        "channel": channel,
        "account": account,
        "idempotency_key": key,
    }
    order_id = persistence.save_order(order)
    if req.dry_run:
        return {"order_id": order_id, "state": "DRY_RUN", "idempotency_key": key}
    res = execution_runtime.submit_order(order_id)
    return {"order_id": order_id, "idempotency_key": key, **res}


@router.get("/orders/by-key", tags=["execution"])
def lookup_order_by_key(key: str) -> dict:
    """按**幂等键**查单（只读）。

    ★ 2026-11-06 接线：`persistence.find_order_by_key` 此前**无任何调用方**（死代码），
      "按幂等键查单"这个排查重复下单/丢单的刚需实际不可用。现开放为只读端点。
    """
    from app.execution import persistence

    row = persistence.find_order_by_key(key)
    return {"found": row is not None, "order": row}


@router.get("/orders/by-signal", tags=["execution"])
def lookup_order_by_signal(
    real_symbol: str,
    signal_id: str,
    account: Optional[str] = None,
    unsettled_only: bool = False,
) -> dict:
    """按 ``(account, real_symbol, signal_id)`` 查最近一笔订单（只读）。

    幂等键含交易日 → 跨天不再幂等；本端点给出**跨天**维度的查单能力。
    """
    from app.execution import persistence

    row = persistence.find_order_by_signal(
        account, real_symbol, signal_id, only_unsettled=unsettled_only)
    return {"found": row is not None, "order": row}


@router.get("/orders", tags=["execution"])
def list_orders(limit: int = 50, status: Optional[str] = None) -> dict:
    """列最近订单（只读，不受实盘门禁，便于观测）。"""
    from sqlalchemy import text

    with session_scope() as s:
        sql = "SELECT * FROM execution_order"
        params: dict = {}
        if status:
            sql += " WHERE status = :st"
            params["st"] = status
        sql += " ORDER BY id DESC LIMIT :n"
        params["n"] = max(1, min(int(limit), 500))
        rows = s.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "orders": [dict(r) for r in rows]}


@router.get("/positions", tags=["execution"])
def list_positions(real_symbol: Optional[str] = None) -> dict:
    """列当日持仓快照（只读）。"""
    from sqlalchemy import text

    with session_scope() as s:
        sql = "SELECT * FROM execution_position WHERE snapshot_date = CURRENT_DATE"
        params: dict = {}
        if real_symbol:
            sql += " AND real_symbol = :sym"
            params["sym"] = real_symbol
        sql += " ORDER BY real_symbol"
        rows = s.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "positions": [dict(r) for r in rows]}


@router.get("/health", tags=["execution"])
def execution_health() -> dict:
    """通道健康 + 当前网关模式（明确标注是否仿真，避免误认实盘）。"""
    from app.execution import monitor

    return monitor.health_snapshot()
