# -*- coding: utf-8 -*-
"""V5X t-1 门控口径 + 小时扫描器（C3-min / CL-20261108-08）。

这组测试只钉「行为」，不碰数据库：
  1. ``gate_lag=1`` 的三道日频门不得使用当日日线（否则小时级实盘无法复现）；
  2. 门不能因为加 lag 而失效（昨日极端仍须判出当日禁开）；
  3. ``frozen + gate_lag=1`` 必须 fail loud；
  4. 推送文案里的价格必须是**反解后的真实价**，不能是 BA 价。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.strategies.v5x.context import (
    _cluster_shock,
    _hvol_flags,
    _regime_table,
    load_market_data,
)
from app.strategies.v5x.live import V5XSignal, format_message

N = 320
TD = list(range(N))
SH = ZoneInfo("Asia/Shanghai")


#: 用真实簇内品种：簇冲击只对 CLUSTER 里登记过的品种生效，
#: 随便编一个 "X888" 会让 g["cl"] 全为 NaN 被 dropna 掉，测试恒定「不封禁」。
SYM = "RB888"


def _daily(amp, close, vol, lret=None):
    """``lret`` 必须由 _daily_frame 在真实价空间算好（CL-20261108-25）；
    这里默认给 0 填充，仅簇冲击用例需要显式传入。"""
    return pd.DataFrame({"symbol": SYM, "td": TD, "amp": amp,
                         "c": close, "vol": vol,
                         "lret": [0.0] * len(TD) if lret is None else lret})


# ---------------------------------------------------------------- hvol
def test_hvol_lag0_uses_today():
    """回测口径：当日振幅极端 → 当日即判禁开。"""
    d = _daily([0.01] * (N - 1) + [0.99], [100.0] * N, [0.01] * N)
    assert bool(_hvol_flags(d, 0)[SYM][N - 1]) is True


def test_hvol_lag1_ignores_today():
    """t-1 口径：当日振幅极端**不得**影响当日判定（否则盘中算不出来）。"""
    d = _daily([0.01] * (N - 1) + [0.99], [100.0] * N, [0.01] * N)
    assert bool(_hvol_flags(d, 1)[SYM][N - 1]) is False


def test_hvol_lag1_still_fires_on_previous_day():
    """t-1 口径：昨日极端 → 今日必须禁开（门没被 lag 关掉）。"""
    d = _daily([0.01] * (N - 2) + [0.99, 0.01], [100.0] * N, [0.01] * N)
    assert bool(_hvol_flags(d, 1)[SYM][N - 1]) is True


# ---------------------------------------------------------------- 簇冲击
def test_cluster_shock_lag0_uses_today():
    """``_cluster_shock`` 的返回是 {簇名: 封禁日集合}，不是按品种。"""
    d = _daily([0.01] * N, [100.0] * N, [0.01] * N,
               lret=[0.0] * (N - 1) + [0.5])
    assert (N - 1) in _cluster_shock(d, 0).get("黑色", set())


def test_cluster_shock_lag1_ignores_today():
    d = _daily([0.01] * N, [100.0] * N, [0.01] * N,
               lret=[0.0] * (N - 1) + [0.5])
    assert (N - 1) not in _cluster_shock(d, 1).get("黑色", set())


# ---------------------------------------------------------------- 体制
def test_regime_lag0_uses_today_vol():
    d = _daily([0.01] * N, [100.0] * N, [0.01] * (N - 1) + [9.9])
    assert _regime_table(d, "strict", 0)[(SYM, N - 1)][0] == 2


def test_regime_lag1_ignores_today_vol():
    d = _daily([0.01] * N, [100.0] * N, [0.01] * (N - 1) + [9.9])
    assert _regime_table(d, "strict", 1)[(SYM, N - 1)][0] != 2


# ---------------------------------------------------------------- 组合校验
def test_frozen_with_lag_rejected():
    """frozen（全样本分位，含前视）叠 t-1 会得到第三种口径，必须抛错。"""
    with pytest.raises(ValueError):
        _regime_table(_daily([0.01] * N, [100.0] * N, [0.01] * N), "frozen", 1)


def test_load_market_data_rejects_bad_gate_lag():
    with pytest.raises(ValueError):
        load_market_data(None, [], date(2026, 1, 1), date(2026, 2, 1), gate_lag=2)


def test_load_market_data_rejects_frozen_with_lag():
    with pytest.raises(ValueError):
        load_market_data(None, [], date(2026, 1, 1), date(2026, 2, 1),
                         regime_mode="frozen", gate_lag=1)


# ---------------------------------------------------------------- 推送文案
def test_message_uses_real_price_not_ba():
    """BA 价可为负（JD888 实测 -462），推送正文必须只出现真实价。"""
    sig = V5XSignal(symbol="JD888", action="OPEN", side="SHORT",
                    bar_ts=datetime(2026, 11, 6, 23, 0, tzinfo=SH),
                    adj_offset=-3923.0, price_ba=-462.0, price_real=3461.0,
                    lots=1.0, stop_ba=-412.0, stop_real=3511.0, etype="bk")
    title, body = format_message([sig])
    assert "开1" in title
    assert "3,461.00" in body          # 真实价必须出现
    assert "-462.00" in body           # BA 价仅作括号备注，不能当作成交价
    assert "3,511.00" in body          # 止损也须反解
    assert "只推信号" in body           # 明确「不建单」
