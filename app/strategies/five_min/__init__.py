# -*- coding: utf-8 -*-
"""5m 策略包（CL-20261121-04/05/06）。

边界：本包只放**新建**的 5m 策略与门控；不修改 V5X / V3.4 的既有口径。
所有能力默认关闭（开关 + 显式 pool_id），未启用时现网行为逐位不变。
"""
from __future__ import annotations

from app.strategies.five_min.pool_position import PoolPositionGuard

__all__ = ["PoolPositionGuard"]
