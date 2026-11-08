# -*- coding: utf-8 -*-
"""V5X.3 策略包（CL-20261108-22 / C1）。

模块划分
--------
* ``engine``  —— 冻结参考引擎，逐位移植，逻辑一字未改（E1 层）
* ``params``  —— 冻结参数第二真源 + 改参数的唯一入口（overrides 显式传入）
* ``context`` —— DailyCtx 的数据供给（hvol / member_z / 簇冲击 / regime / 会话末根）

边界
----
本包**只做回测与研究**，不接实盘推送/下单链路。V3.4 仍是生产策略：
`app/strategies/fusion_signal.py` 与 `app/backtest/fusion_backtest.py` 完全独立。
"""
from __future__ import annotations

from app.strategies.v5x.engine import (
    CLUSTER,
    P4_SECTORS,
    CostCfg,
    DailyCtx,
    SymbolEngine,
    V5XConfig,
)
from app.strategies.v5x.params import FROZEN_V5X3, check_defaults, config_from

__all__ = [
    "CLUSTER",
    "P4_SECTORS",
    "CostCfg",
    "DailyCtx",
    "SymbolEngine",
    "V5XConfig",
    "FROZEN_V5X3",
    "check_defaults",
    "config_from",
]
