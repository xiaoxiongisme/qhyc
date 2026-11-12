# -*- coding: utf-8 -*-
"""CL-20261112-06 P0-2：组合熔断绝不能把平仓手数清零。

回归背景：S1 接线时 ``_apply_v5x_portfolio_brake`` 无差别缩放 ``for g in sigs``，
``scalar=0`` 会把 CLOSE 的 lots 置 0，并被 ``_persist`` 写进 ``v5x_signal_log``。
S2（补齐 20 个交易日真实净值）一旦激活必然发作，而 CLOSE 文案当时还不展示手数。
"""
from types import SimpleNamespace
from unittest.mock import patch

from app.strategies.v5x.live import (
    BRAKE_EXEMPT_ACTIONS,
    _apply_v5x_portfolio_brake,
    scale_signal_lots,
)


def _sig(action: str, lots: float = 2.0):
    return SimpleNamespace(action=action, lots=lots, symbol="RB888",
                           side="LONG", dedupe_key=f"k-{action}")


def test_close_action_is_exempt():
    assert "CLOSE" in BRAKE_EXEMPT_ACTIONS
    assert "OPEN" not in BRAKE_EXEMPT_ACTIONS
    assert "ADD" not in BRAKE_EXEMPT_ACTIONS


def test_scalar_zero_keeps_close_lots_and_zeroes_open():
    open_, add_, close_ = _sig("OPEN"), _sig("ADD"), _sig("CLOSE")
    n_scaled, n_exempt = scale_signal_lots([open_, add_, close_], 0.0)
    assert (n_scaled, n_exempt) == (2, 1)
    assert open_.lots == 0.0
    assert add_.lots == 0.0
    assert close_.lots == 2.0, "平仓手数被清零 = 清仓观望时禁止离场"


def test_half_scalar_rounds_up_and_never_below_one():
    sigs = [_sig("OPEN", 3.0), _sig("ADD", 1.0), _sig("CLOSE", 1.5)]
    n_scaled, n_exempt = scale_signal_lots(sigs, 0.5)
    assert (n_scaled, n_exempt) == (2, 1)
    assert sigs[0].lots == 2.0        # ceil(3*0.5)=2
    assert sigs[1].lots == 1.0        # ceil(1*0.5)=1，下限 1 手
    assert sigs[2].lots == 1.5


def test_identity_scalar_is_noop():
    sigs = [_sig("OPEN"), _sig("CLOSE")]
    assert scale_signal_lots(sigs, 1.0) == (0, 0)
    assert [s.lots for s in sigs] == [2.0, 2.0]


def test_consumer_uses_pure_function_and_reports_exempt():
    """消费点必须真的走纯函数（防「函数写好了没人调用」）。"""
    state = SimpleNamespace(enabled=True, idle=False, scalar=0.0,
                            reason="ok", n_days=25, need_days=20)
    with patch("app.risk.portfolio_brake.brake_state_for_session",
               return_value=state) as mocked:
        sigs = [_sig("OPEN"), _sig("CLOSE")]
        note, summary = _apply_v5x_portfolio_brake(object(), sigs)
    assert mocked.called
    assert sigs[0].lots == 0.0 and sigs[1].lots == 2.0
    assert summary["n_scaled"] == 1 and summary["n_exempt_close"] == 1
    assert "平仓信号手数保持不变" in note
