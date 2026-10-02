# -*- coding: utf-8 -*-
"""P0-1 组件③：手数计算与校验。

- 模式 A：信号给定 lots → 校验 ≥1 整数 + 不超上限
- 模式 B：目标风险 R → 反算整数手
名义金额 = lots × multiplier × price
"""
from __future__ import annotations

import math
from typing import Optional

#: 单品种最大手数（可配：环境变量 MAX_LOTS_PER_PRODUCT）
MAX_LOTS = int(__import__("os").getenv("MAX_LOTS_PER_PRODUCT", "10"))


def validate_given_lots(lots: Optional[int]) -> tuple[int, list[str]]:
    """校验信号给定手数，返回 (lots, warnings)。"""
    warns: list[str] = []
    if lots is None:
        return 0, ["lots 未提供"]
    if lots < 1:
        return 0, [f"lots={lots} < 1，非法"]
    if lots > MAX_LOTS:
        warns.append(f"lots={lots} 超过单品种上限 {MAX_LOTS}")
    return lots, warns


def from_target_risk(target_risk: float, atr: float, multiplier: float,
                     price: float) -> tuple[int, list[str]]:
    """按目标风险 R 反算手数：lots = floor(R / (atr × multiplier))。

    [CB 待办] 合约单位风险口径：此处用 atr × multiplier（每手每点价值）；
    若需含保证金/杠杆约束，后续接入账户实时权益。
    返回 (lots, warnings)。
    """
    warns: list[str] = []
    if atr is None or atr <= 0:
        return 0, ["atr 缺失或非正，无法按风险算手数"]
    if multiplier is None or multiplier <= 0:
        return 0, ["multiplier 缺失，无法算手数"]
    per_lot_risk = atr * multiplier
    lots = int(math.floor(target_risk / per_lot_risk))
    if lots < 1:
        warns.append(f"目标风险 {target_risk} 元 < 单手风险 {per_lot_risk:.0f} 元，至少 1 手")
        lots = 1
    if lots > MAX_LOTS:
        warns.append(f"反算手数 {lots} 超过上限 {MAX_LOTS}，截断")
        lots = MAX_LOTS
    return lots, warns
