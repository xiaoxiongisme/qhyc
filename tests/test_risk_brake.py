# -*- coding: utf-8 -*-
"""PRD V5 验收：组合级回撤熔断（历史净值回放）。

验收点（D2 阈值）：
  * 回撤 > 15% 且 ≤ 25% → scalar = 0.5（降半仓）
  * 回撤 > 25%          → scalar = 0.0（清仓观望）
  * 回撤 ≤ 15%          → scalar = 1.0（不动）
  * enabled=False       → 恒为 1.0，与"从未上线"逐位等价（双向等价性）

不依赖数据库：`brake_scalar` / `apply` 是纯函数。
"""
from __future__ import annotations

import pytest

from app.risk.portfolio_brake import (
    BrakeConfig,
    apply,
    brake_scalar,
    current_drawdown,
    max_drawdown,
)


def _curve(peak, trough_series):
    """构造权益曲线：先到 peak，再按 trough_series 回撤。"""
    eq = [float(peak)]
    for t in trough_series:
        eq.append(float(t))
    return eq


def test_max_drawdown_basic():
    assert abs(max_drawdown([100, 120, 90, 110, 60]) - 0.5) < 1e-9   # 120→60 = -50%
    assert max_drawdown([]) == 0.0


def test_current_dd_warn_band():
    """回撤落在 (15%, 25%] → 0.5。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    # 100 → 82：回撤 18%（在 15%~25% 之间）
    eq = _curve(100, [82])
    assert abs(current_drawdown(eq) - 0.18) < 1e-9
    assert brake_scalar(eq, cfg) == 0.5


def test_current_dd_stop_band():
    """回撤 > 25% → 0.0。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    eq = _curve(100, [70])  # 回撤 30%
    assert current_drawdown(eq) > 0.25
    assert brake_scalar(eq, cfg) == 0.0


def test_current_dd_normal():
    """回撤 ≤ 15% → 1.0。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    eq = _curve(100, [90])  # 回撤 10%
    assert current_drawdown(eq) <= 0.15
    assert brake_scalar(eq, cfg) == 1.0


def test_apply_scales_lots():
    """apply：期望手数按标量缩放。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    # 半仓：2 手 → 1 手
    assert apply(2.0, _curve(100, [82]), cfg) == 1.0
    # 清仓：5 手 → 0 手
    assert apply(5.0, _curve(100, [70]), cfg) == 0.0
    # 正常：3 手 → 3 手
    assert apply(3.0, _curve(100, [90]), cfg) == 3.0


def test_disabled_is_identity():
    """enabled=False 恒等：scalar≡1.0，与从未上线逐位等价。"""
    eq = _curve(100, [70])  # 即便回撤 30% 也该清仓，但关闭时不动
    cfg_off = BrakeConfig(enabled=False)
    assert brake_scalar(eq, cfg_off) == 1.0
    assert apply(5.0, eq, cfg_off) == 5.0


def test_replay_history_scenarios():
    """历史净值回放：三段情景覆盖三档阈值。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    scenarios = [
        (_curve(200, [185]), 1.0),   # -7.5%
        (_curve(200, [165]), 0.5),   # -17.5%
        (_curve(200, [140]), 0.0),   # -30%
    ]
    for eq, expected in scenarios:
        assert brake_scalar(eq, cfg) == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
