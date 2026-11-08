# -*- coding: utf-8 -*-
"""BA 空间比例运算修复（CL-20261108-25）。

``l2_adj.bar_60m_ba`` 是**等差**后复权（BA = 真实 + offset）。等差偏移的平移不变量
只有**差值**（EMA / ATR / 枢轴 / 点数），振幅率与对数收益**都不是**平移不变的。
所以日频派生必须回真实价做。

这组测试钉四件事：
  1. BA 价为负时，``amp`` 仍等于真实价的振幅（旧实现会符号翻转）；
  2. ``high/low`` 与 ``close`` 同量平移、减同一个偏移；
  3. 换月根的 60m 收益被置 NaN，不污染日已实现波动 σ；
  4. ``_cluster_shock`` 不接受「自己用 BA 收盘取 log」的调用方式（显式报错）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategies.v5x.context import _cluster_shock, _daily_frame

DAY1 = "2024-01-02"
DAY2 = "2024-01-03"
OPEN_DAYS = np.array([np.datetime64(DAY1), np.datetime64(DAY2)])
REAL = np.array([100.0, 99.0, 98.0, 97.0])
OFF = np.array([-3300.0, -3300.0, -3300.0, -3400.0])   # 最后一根换月


def _bars(real=REAL, off=OFF) -> pd.DataFrame:
    n = len(real)
    buckets = pd.to_datetime([f"{DAY1} 10:00", f"{DAY1} 11:00",
                              f"{DAY1} 14:00", f"{DAY2} 10:00"])[:n]
    ba = real + off
    return pd.DataFrame({
        "symbol": ["FB888"] * n,
        "bucket": buckets,
        "open": ba,
        "high": real + 1.0 + off,
        "low": real - 1.0 + off,
        "close": ba,
        "volume": np.ones(n),
        "raw_c": real,
    })


def test_ba_prices_really_negative():
    """前提：这组数据在 BA 空间确实是负价（模拟真实库里的 FB888）。"""
    assert (_bars()["close"] <= 0).all()


def test_amp_uses_real_price_not_ba():
    """负价 BA 下振幅必须是正的真实振幅，旧实现会得到负数。"""
    d = _daily_frame(_bars(), OPEN_DAYS)
    d1 = d[d["td"].astype(str) == DAY1].iloc[0]
    assert d1["c"] == pytest.approx(98.0)          # 收盘 = 真实价
    assert d1["hh"] == pytest.approx(101.0)        # 高 = 真实高
    assert d1["ll"] == pytest.approx(97.0)         # 低 = 真实低
    assert d1["amp"] == pytest.approx(4.0 / 98.0)
    assert d1["amp"] > 0

    # 旧口径：同一数据在 BA 空间算 (hh-ll)/c = 4 / -3202 → 负振幅
    old_amp = 4.0 / -3202.0
    assert old_amp < 0
    assert d["amp"].min() > 0, "修复后不允许出现负振幅"


def test_roll_day_lret_is_nan():
    """跨换月的交易日，日对数收益必须为 NaN（否则换月跳空冒充真实收益）。"""
    d = _daily_frame(_bars(), OPEN_DAYS)
    d2 = d[d["td"].astype(str) == DAY2].iloc[0]
    assert pd.isna(d2["lret"])


def test_vol_ignores_roll_bar():
    """当日 60m 收益的 σ 不得被换月根的跳空污染。"""
    d = _daily_frame(_bars(), OPEN_DAYS)
    d2 = d[d["td"].astype(str) == DAY2].iloc[0]
    assert pd.isna(d2["vol"]), "换月日只剩 1 根有效 bar，σ 应为 NaN 而非被跳空撑大"


def test_cluster_shock_rejects_missing_lret():
    """防止回退：``_cluster_shock`` 不得再自己用 BA 收盘取 log。"""
    daily = pd.DataFrame({"symbol": ["RB888"], "td": [1], "c": [-3200.0],
                          "amp": [0.01], "vol": [0.01]})
    with pytest.raises(ValueError, match="lret"):
        _cluster_shock(daily, 0)


def test_normal_positive_ba_unchanged():
    """BA 全为正时，新旧口径的振幅应当一致（回归保护：不要改坏正常品种）。"""
    off = np.array([10.0, 10.0, 10.0, 10.0])
    bars = _bars(off=off)
    assert (bars["close"] > 0).all()
    d = _daily_frame(bars, OPEN_DAYS)
    d1 = d[d["td"].astype(str) == DAY1].iloc[0]
    # hh - ll 在平移下不变，比例则要回真实价
    assert d1["hh"] - d1["ll"] == pytest.approx(4.0)
    assert d1["amp"] == pytest.approx(4.0 / 98.0)
