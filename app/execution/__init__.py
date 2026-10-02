# -*- coding: utf-8 -*-
"""P0-1 执行反解层（信号 → 真实合约可执行订单）。

依赖：app.data.back_adjust / app.repositories.main_contract_repo /
      app.repositories.symbol_repo / app.core.symbol_code。
PRD：docs/PRD_P0-1_执行反解层_20261002.md
"""
from app.execution.schemas import ExecutionOrder, ReverseRequest
from app.execution.service import ReverseResolutionError, resolve_signal_to_order

__all__ = [
    "ReverseRequest",
    "ExecutionOrder",
    "resolve_signal_to_order",
    "ReverseResolutionError",
]
