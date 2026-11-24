# -*- coding: utf-8 -*-
"""CL-20261124-06：5m 离场执行链路纯函数单测（不依赖 DB/网络）。"""
import pytest

from app.strategies.five_min.exit_executor import (
    build_exit_plan, to_guard_plan, decide,
)


def _plan(direction="SHORT", entry=4306.0, r=37.57, lots=3):
    """BU888 空头样例：entry=4306, 1R=3ATR, TP=2.5R。"""
    sign = 1 if direction == "LONG" else -1
    init_stop = entry - sign * r  # 空头 stop 在 entry+r 上方
    return build_exit_plan(
        signal_id="5M:BU888:2026-11-16 13:05:00+00:00",
        open_order_id=259, symbol_continuous="BU888", real_symbol="BU2701",
        exchange="SHFE", direction=direction, lots=lots,
        entry_px=entry, init_stop=init_stop, r_unit=r,
    )


def test_build_plan_caliber():
    p = _plan("SHORT")
    assert p["direction"] == "SHORT"
    assert p["tp_px"] == round(4306.0 - 2.5 * 37.57, 6)  # 空头目标在下方
    assert p["lots"] == 3


def test_build_plan_rejects_bad_tp_r():
    with pytest.raises(ValueError, match="2.5R"):
        build_exit_plan(
            signal_id="x", open_order_id=1, symbol_continuous="BU888",
            real_symbol="BU2701", exchange="SHFE", direction="SHORT",
            lots=1, entry_px=100, init_stop=101, r_unit=1, tp_r=2.0)


def test_build_plan_rejects_r_mismatch():
    with pytest.raises(ValueError, match="1R 口径不一致"):
        build_exit_plan(
            signal_id="x", open_order_id=1, symbol_continuous="BU888",
            real_symbol="BU2701", exchange="SHFE", direction="SHORT",
            lots=1, entry_px=100, init_stop=105, r_unit=1)  # |100-105|=5 ≠ 1


def test_guard_plan_disables_tp1_time_and_trail():
    gp = to_guard_plan(_plan())
    assert gp["tp1_px"] is None and gp["tp1_lots"] is None
    assert gp["time_stop_at"] is None and gp["trail_stop"] is None
    assert gp["rest_lots"] == 0
    assert gp["tp2_lots"] == 3  # 全手数，不分批


def test_decide_short_hard_stop():
    # 空头：价格上涨触止损（entry+r=4343.57）
    assert decide(_plan(), last_price=4343.57) == "HARD_STOP"
    assert decide(_plan(), last_price=4350.0) == "HARD_STOP"


def test_decide_short_tp():
    # 空头：价格下跌触止盈（entry-2.5r=4212.075）
    assert decide(_plan(), last_price=4212.07) == "TP"
    assert decide(_plan(), last_price=4200.0) == "TP"


def test_decide_short_no_trigger():
    # 区间内不触发
    assert decide(_plan(), last_price=4280.0) is None


def test_decide_long_symmetry():
    p = _plan("LONG", entry=5000.0, r=30.0)
    # 多头：stop=4970, tp=5075
    assert decide(p, last_price=4969.0) == "HARD_STOP"
    assert decide(p, last_price=5076.0) == "TP"
    assert decide(p, last_price=5020.0) is None


def test_decide_hard_stop_priority():
    # 空头：极端高价同时越过 stop 与（不可能的）tp，防守优先判 HARD_STOP
    assert decide(_plan(), last_price=9999.0) == "HARD_STOP"
