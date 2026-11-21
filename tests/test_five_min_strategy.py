# -*- coding: utf-8 -*-
"""P1 · 5m 策略框架单测（mock 数据，不连生产库）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.five_min.engine import ExitPlan, lot_size, run_replay, summarize
from app.strategies.five_min.indicators import attach_indicators, trading_date
from app.strategies.five_min.signals import Signal, scan_signals


def _ctx(n=60, close=None, atr=1.0, start="2026-11-20 09:00"):
    dt = pd.date_range(start, periods=n, freq="5min")
    c = close if close is not None else np.full(n, 100.0)
    c = np.asarray(c, dtype=float)
    return pd.DataFrame({
        "dt": dt, "open": c, "high": c + 0.6, "low": c - 0.6, "close": c,
        "ema_f": c, "ema_s": c - 2, "dif": np.ones(n) * 0.2, "dea": np.zeros(n),
        "hist": np.ones(n) * 0.2, "atr": np.full(n, atr), "daily_dir": np.ones(n),
    })


def test_indicators_use_previous_trading_day_only():
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21"]),
        "close": [100.0] * 4,
    })
    bars = pd.DataFrame({
        "dt": pd.to_datetime(["2026-11-20 10:00", "2026-11-21 10:00"]),
        "open": [1.0, 1.0], "high": [2.0, 2.0], "low": [0.5, 0.5], "close": [1.0, 1.0],
    })
    out = attach_indicators(bars, daily)
    # 当日日线不能提前参与：11-20 的 bar 只能看 11-19，11-21 的 bar 看 11-20。
    assert list(out["daily_pos"]) == [1, 2]
    assert str(trading_date(pd.Timestamp("2026-11-21 01:30"))) == "2026-11-20 00:00:00"


def test_signals_are_eventized_and_directional():
    n = 40
    ctx = _ctx(n)
    ctx.loc[10, "close"] = ctx.loc[9, "close"] - 2   # bar10 close 上穿 ema_f(=close 旧值)? 下面直接造
    # 用 close 上穿 ema_f 制造两个事件，其余 bar 保持状态（不应重复触发）
    ctx["ema_f"] = 100.0
    ctx["ema_s"] = 98.0
    closes = np.full(n, 99.0)
    closes[10:20] = 101.0          # 10 上穿、20 下穿，中间持续在上方
    closes[21:] = 101.0            # 21 再上穿
    ctx["close"] = closes
    ctx["hist"] = np.full(n, 0.5)  # hist/atr=0.5 > 0.10
    ctx["daily_dir"] = 1
    sigs = scan_signals(ctx)
    long_idx = [s.index for s in sigs if s.side > 0]
    assert 10 in long_idx and 21 in long_idx
    assert not any(11 <= i <= 19 for i in long_idx)  # 持续状态不重复入场
    # 带方向的 hist：空头需要 -hist/atr > 阈值，hist>0 时不应出空头
    assert not any(s.side < 0 for s in sigs)


def test_signals_respect_hist_atr_threshold_and_session():
    ctx = _ctx(40)
    ctx["ema_f"] = 100.0
    ctx["ema_s"] = 98.0
    closes = np.full(40, 99.0)
    closes[10:] = 101.0
    ctx["close"] = closes
    ctx["hist"] = 0.05             # hist/atr=0.05 < 0.10 → 全被过滤
    assert scan_signals(ctx) == []
    ctx["hist"] = 0.5
    ctx["dt"] = pd.date_range("2026-11-20 21:00", periods=40, freq="5min")
    assert scan_signals(ctx) == []  # 夜盘不在白盘 9–15 内


def test_engine_replays_single_position_with_correct_caliber():
    c = np.full(60, 100.0)
    c[11] = 100.0
    c[12] = 108.0                  # 收盘越过 tp=107.5
    c[20] = 100.0
    c[21] = 108.0
    ctx = _ctx(close=c, atr=1.0)
    sigs = [Signal(10, 1, 0.2, "trend_pullback"),
            Signal(11, 1, 0.2, "trend_pullback"),  # 在场信号须忽略
            Signal(19, 1, 0.2, "macd_cross"),
            Signal(20, 1, 0.2, "macd_cross")]
    tr = run_replay(ctx, sigs, "FG888", mult=10, tick=1, fee_per_side=3.0)
    assert len(tr) == 2 and tr[0]["bars"] == 1 and tr[0]["reason"] == "TP"
    assert tr[0]["lots"] == 16      # floor(500/(3*1*10))
    expected_cost = 2 * (3 + 1 * 10) * 16
    assert tr[0]["cost"] == expected_cost
    assert tr[0]["pnl"] == 16 * 10 * 8.0 - expected_cost
    assert tr[0]["exit_plan"]["r_unit"] == 3.0
    assert tr[0]["exit_plan"]["tp2_r"] == 2.5
    assert tr[0]["exit_plan"]["tp1_r"] is None
    assert summarize(tr)["n"] == 2


def test_limit_fill_requires_touch():
    c = np.full(30, 100.0)
    ctx = _ctx(30, close=c)
    sig = [Signal(5, 1, 0.2, "macd_cross")]
    # 次根区间在未复权信号价之上 → 不成交
    ctx.loc[6, ["high", "low"]] = [105.0, 102.0]
    assert run_replay(ctx, sig, "FG888", mult=10, tick=1, fee_per_side=3.0,
                      fill_mode="limit") == []
    # 次根触及 100 → 挂单价成交
    ctx.loc[6, ["high", "low"]] = [105.0, 99.0]
    tr = run_replay(ctx, sig, "FG888", mult=10, tick=1, fee_per_side=3.0,
                    fill_mode="limit")
    assert len(tr) == 1 and tr[0]["entry_px"] == 100.0


def test_cost_scales_with_contract_multiplier():
    c = np.full(30, 100.0)
    ctx = _ctx(30, close=c)
    sig = [Signal(5, 1, 0.2, "macd_cross")]
    a = run_replay(ctx, sig, "MULT1", mult=1, tick=1, fee_per_side=3.0)[0]
    b = run_replay(ctx, sig, "MULT10", mult=10, tick=1, fee_per_side=3.0)[0]
    assert a["lots"] == 166 and b["lots"] == 16
    assert a["cost"] == 2 * (3 + 1 * 1) * 166
    assert b["cost"] == 2 * (3 + 1 * 10) * 16
    assert b["cost"] != 2 * (3 + 1) * 16  # 复现「漏乘 mult」的旧 bug 会被这条抓住


def test_exit_plan_caliber_guard():
    plan = ExitPlan(entry_px=100.0, side=-1, atr=2.0, lot_size=5)
    assert plan.r_unit == 6.0 and plan.stop_px == 106.0 and plan.target_px == 85.0
    assert plan.tp_r == 2.5 and lot_size(500, 2, 10, 3) == 8
