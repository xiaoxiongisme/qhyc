# -*- coding: utf-8 -*-
"""P3 校准模块单测（mock 数据，不依赖生产库）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategies.five_min.calibration import (
    calibrate_touch,
    fill_rate_stress_replay,
    _entry_events,
)
from app.strategies.five_min.indicators import DEFAULT_CFG
from app.strategies.five_min.signals import Signal


def _ctx_with_one_signal(n: int = 40, sig_i: int = 30, sig_px: float = 100.0,
                         next_bars: tuple[float, float, float] | None = None) -> tuple[pd.DataFrame, list[Signal], dict]:
    """构造一根信号 bar + 次根，其余为平稳序列；返回 ctx/signals/cfg。"""
    idx = pd.date_range("2025-01-01 09:00", periods=n, freq="5min")
    close = np.full(n, sig_px, dtype=float)
    high = close + 0.5
    low = close - 0.5
    open_ = close.copy()
    atr = np.full(n, 1.0, dtype=float)   # ATR=1 → R=500/(3*1*mult) 手
    if next_bars is not None:
        no, nh, nl = next_bars
        open_[sig_i + 1] = no
        high[sig_i + 1] = max(nh, no)
        low[sig_i + 1] = min(nl, no)
    ctx = pd.DataFrame({"dt": idx, "open": open_, "high": high,
                        "low": low, "close": close, "atr": atr})
    sig = Signal(sig_i, 1, 0.2, "macd_cross", idx[sig_i])
    cfg = dict(DEFAULT_CFG)
    cfg["mult"] = 10.0
    cfg["tick"] = 1.0
    cfg["session"] = (0, 23)   # 全时段，避免白盘门干扰 synthetic 时间
    return ctx, [sig], cfg


def test_touch_rate_all_touched():
    # 次根 high/low 覆盖信号价 → 触及
    ctx, sigs, cfg = _ctx_with_one_signal(next_bars=(100.0, 101.0, 99.0))
    r = calibrate_touch(ctx, sigs, mult=10.0, tick=1.0, cfg=cfg)
    assert r["attempts"] == 1
    assert r["touched"] == 1
    assert r["touch_rate"] == 1.0
    assert r["gap_ticks_mean"] == pytest.approx(0.0)  # open=信号价, gap=0


def test_touch_rate_none_touched_gap_positive():
    # 次根整体高于信号价（买入需回落）→ 未触及且开盘 gap 不利为正
    ctx, sigs, cfg = _ctx_with_one_signal(next_bars=(103.0, 105.0, 102.0))
    r = calibrate_touch(ctx, sigs, mult=10.0, tick=1.0, cfg=cfg)
    assert r["attempts"] == 1
    assert r["touched"] == 0
    assert r["expired"] == 1
    assert r["touch_rate"] == 0.0
    # 未触及不计入 gap（gap 只统计成交机会），保持 0
    assert r["gap_ticks_mean"] == 0.0


def test_gap_adverse_direction():
    # 买入：next open 高于信号价 2 tick → gap=+2（不利）
    ctx, sigs, cfg = _ctx_with_one_signal(next_bars=(102.0, 103.0, 100.0))
    r = calibrate_touch(ctx, sigs, mult=10.0, tick=1.0, cfg=cfg)
    assert r["touched"] == 1
    assert r["gap_ticks_mean"] == pytest.approx(2.0)


def test_entries_respect_single_position():
    # 两个信号：第一个触及后进入持仓，第二个若在持仓期内应被吞掉
    idx = pd.date_range("2025-01-01 09:00", periods=60, freq="5min")
    close = np.full(60, 100.0)
    high, low, open_ = close + 0.5, close - 0.5, close.copy()
    atr = np.full(60, 1.0)
    # 信号1 在 i=10，次根触及；因止损/止盈都远（3ATR=3, 7.5ATR=7.5），close 恒 100 不触发，
    # 直到 EOD 才平 —— 其间 i=20 的信号2 必被吞掉
    sigs = [Signal(10, 1, 0.2, "macd_cross", idx[10]),
            Signal(20, 1, 0.2, "macd_cross", idx[20])]
    cfg = dict(DEFAULT_CFG)
    cfg["mult"] = 10.0
    ctx = pd.DataFrame({"dt": idx, "open": open_, "high": high,
                        "low": low, "close": close, "atr": atr})
    entries = _entry_events(ctx, sigs, mult=10.0, cfg=cfg)
    assert len(entries) == 1  # 只有一个真正入场机会


def test_fill_rate_stress_monotone_n():
    # 保留率越低，平均笔数不应增加
    ctx, sigs, cfg = _ctx_with_one_signal(n=60, next_bars=(100.0, 101.0, 99.0))
    r = fill_rate_stress_replay(ctx, sigs, "TEST", mult=10.0, tick=1.0,
                                fee_per_side=3.0, cfg=cfg)
    assert r["keep_100"]["n"] >= r["keep_80"]["n"] >= r["keep_40"]["n"]
    assert set(r) == {"keep_100", "keep_80", "keep_60", "keep_40"}
