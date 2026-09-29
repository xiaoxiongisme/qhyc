# -*- coding: utf-8 -*-
"""融合状态机 × 因子偏置乘子：集成边界测试（PRD §5 / T18 双向等价）。

只验证「接线是否真的生效」，不依赖数据库：
  * 正向：因子禁用(ctx 无启用 B) ≡ 因子不存在(无 symbol/trade_date)
          → state 序列逐位一致（且基线确有开仓，否则"一致"是假绿）。
  * 反向：启用且有数据(偏空权重) → entry_gate 转负 → 开仓被 gate 掉
          → state 序列与基线明显不同。

短路入口 `get_bias_multipliers` 保证默认（无启用因子）路径近乎零成本：
ctx=None / 缺 symbol·trade_date / ctx 无启用 B 因子 时直接返回 NEUTRAL_BIAS，
跳过一切 DB 查找；只确有启用 B 因子才 lookup。
"""
from __future__ import annotations

import datetime as dt
import types

import numpy as np
import pytest

from app.factor.asof import BiasConfig
from app.strategies.fusion_signal import (
    NEUTRAL_BIAS,
    get_bias_multipliers,
    walk_fusion_states,
)

SYMBOL = "RB888"
TRADE_DATE = dt.date(2026, 1, 5)


# ---------------------------------------------------------------------------
# 参数与行情（极小、确定性、不依赖外部数据）
# ---------------------------------------------------------------------------

def _params():
    """walk_fusion_states 所需的最小参数集（默认值与 V3.4 口径一致）。"""
    return types.SimpleNamespace(
        ma_n=20, atr_n=14, adx_n=14, adx_min=0.0, use_sbull=True,
        fib_confl=False, fib_ratios=(0.382, 0.5, 0.618), fib_tol_atr=0.5,
        add_max_lots=1, add_guard_atr=0.0, add_thr_atr=1.0, W=20,
        entry_mode="both_nm", sl_atr=2.0, trail_atr=2.0, be_r=0.5,
        cooldown_bars=0, ema_k=140,
    )


def _market():
    """前段平稳、第 41 根跳涨突破 → 中性乘子下应触发一次开多。

    返回 (o, h, l, c, htf_dir)，htf_dir 恒为 +1（多头方向），
    把测试焦点锁在「因子门控」而非 Higher-Time-Frame 方向判定。
    """
    n = 60
    c = [100.0] * 40 + [200.0] * 20          # 平稳后一根跳涨突破
    o = [ci - 0.5 for ci in c]
    h = [ci + 2.0 for ci in c]
    l = [ci - 2.0 for ci in c]
    htf_dir = np.ones(n, dtype=int)           # ok_l 恒为真
    return o, h, l, c, htf_dir


def _states(symbol=None, trade_date=None):
    o, h, l, c, htf = _market()
    seq = list(walk_fusion_states(o, h, l, c, htf, _params(),
                                  symbol=symbol, trade_date=trade_date))
    return [d["state"] for d in seq]


class _FakeCtx:
    """可注入的 FactorContext 替身：registry 控制「是否启用 B 因子」，lookup 返回注入 z。"""

    def __init__(self, registry, zval):
        self.registry = registry
        self._z = zval

    def lookup_many(self, ids, symbol, trade_date):
        return {i: self._z for i in ids}


def _ctx(enabled: bool = True, weight: float = -1.0, z: float = 1.0):
    reg = {"probe_b": {"factor_id": "probe_b", "name": "probe",
                       "category": "B", "default_weight": weight, "enabled": enabled}}
    return _FakeCtx(reg, z)


# ---------------------------------------------------------------------------
# 1. 正向：因子禁用 ≡ 因子不存在
# ---------------------------------------------------------------------------

def test_disabled_equals_absent(monkeypatch):
    """禁用(ctx 无启用 B) 与 因子不存在(无 symbol) 的 state 序列逐位一致。"""
    # 因子不存在：不传 symbol → get_bias_multipliers 短路到 NEUTRAL_BIAS
    baseline = _states(symbol=None, trade_date=None)
    # 因子禁用：传了 symbol，但 ctx 没有任何启用 B 因子
    monkeypatch.setattr(
        "app.strategies.fusion_signal._get_factor_ctx",
        lambda: _ctx(enabled=False),
    )
    disabled = _states(symbol=SYMBOL, trade_date=TRADE_DATE)

    assert disabled == baseline
    # 反向护栏：基线必须真的开了仓，否则"一致"是假绿（接线没生效也会一致）
    assert 1 in baseline


# ---------------------------------------------------------------------------
# 2. 反向：启用且有数据 → 与基线不同
# ---------------------------------------------------------------------------

def test_enabled_with_data_differs(monkeypatch):
    """启用 B 因子且 z=1.0、权重-1.0 → entry_gate 转负 → 开仓被门控掉。"""
    baseline = _states(symbol=None, trade_date=None)
    assert 1 in baseline  # 基线确实开了多

    monkeypatch.setattr(
        "app.strategies.fusion_signal._get_factor_ctx",
        lambda: _ctx(enabled=True, weight=-1.0, z=1.0),
    )
    # 启用路径会调用 get_settings().factor_bias；用默认 BiasConfig 注入，避免依赖运行配置
    monkeypatch.setattr(
        "app.strategies.fusion_signal.get_settings",
        lambda: types.SimpleNamespace(factor_bias=BiasConfig()),
    )

    enabled = _states(symbol=SYMBOL, trade_date=TRADE_DATE)
    assert enabled != baseline
    assert set(enabled) == {0}   # 全程空仓（gate 把开仓全部挡掉）


# ---------------------------------------------------------------------------
# 3. 短路单测（隔离验证，不驱动整条状态机）
# ---------------------------------------------------------------------------

def test_get_bias_short_circuits_when_no_ctx():
    assert get_bias_multipliers(None, SYMBOL, TRADE_DATE, _params()) == NEUTRAL_BIAS


def test_get_bias_short_circuits_when_no_symbol():
    assert get_bias_multipliers(_ctx(), None, TRADE_DATE, _params()) == NEUTRAL_BIAS


def test_get_bias_short_circuits_when_no_enabled_B():
    # ctx 有因子但 enabled=False → 等同不存在
    assert get_bias_multipliers(_ctx(enabled=False), SYMBOL, TRADE_DATE, _params()) == NEUTRAL_BIAS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
