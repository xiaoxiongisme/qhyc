# -*- coding: utf-8 -*-
"""P0-1 执行反解层 REST 端点：POST /execution/reverse。

把融合策略信号反解为可执行真实合约订单建议（不直接下单）。
鉴权沿用全局 _auth（见 app/api/__init__.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.execution import resolve_signal_to_order
from app.execution.schemas import ExecutionOrder, ReverseRequest

router = APIRouter()


@router.post("/reverse", response_model=ExecutionOrder, tags=["execution"])
def reverse(req: ReverseRequest, db: Session = Depends(fastapi_db_dep)) -> ExecutionOrder:
    """信号(连续888/复权) → 真实合约订单建议。

    返回 ExecutionOrder；blocking_reasons 非空表示该单禁止下发（详见 PRD §3.4）。
    """
    return resolve_signal_to_order(db, req)
