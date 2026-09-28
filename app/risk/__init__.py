# -*- coding: utf-8 -*-
"""执行层风控（六层解耦 · 应用层）。

只放"不动状态机的执行层风控标量"，如 V5 组合回撤熔断。
状态机内部风控属于策略层（`app/strategies/`），不得放这里。
"""

from app.risk.portfolio_brake import (
    BrakeConfig,
    apply,
    brake_scalar,
    current_drawdown,
    load_equity_curve,
    max_drawdown,
)

__all__ = ["BrakeConfig", "brake_scalar", "apply", "max_drawdown",
           "current_drawdown", "load_equity_curve"]
