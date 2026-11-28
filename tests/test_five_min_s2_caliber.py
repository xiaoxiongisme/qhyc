# -*- coding: utf-8 -*-
"""CL-20261128-01 · S2 幸存结构口径单测（纯函数，不依赖 DB/网络）。

钉死四件事：
  1. S2_PRESET 与研究 combo_final_check.CORE[1] 逐位一致（MACD12/26、TP4.5、
     off0.3/wait2、atr_rank0.7、night、EB）；
  2. hist_momentum 族 = hist_up010（ha>0.10 且前根<=0.10） ∧ 日线多 ∧ 夜盘 ∧ 高波动；
  3. 默认两族不被污染（同一 ctx 用 DEFAULT_CFG 不产生 S2 信号）；
  4. 限价偏移成交价 = close - dir*off*ATR（研究 calc_outcome 同源）。
"""
import numpy as np
import pandas as pd
import pytest

from app.strategies.five_min.indicators import (
    DEFAULT_CFG, S2_PRESET, S2_SYMBOLS, attach_indicators, wilder_atr,
)
from app.strategies.five_min.signals import scan_signals, Signal
from app.strategies.five_min.engine import run_replay, ExitPlan


def test_s2_preset_matches_research():
    assert S2_PRESET["macd_fast"] == 12 and S2_PRESET["macd_slow"] == 26
    assert S2_PRESET["macd_signal"] == 9
    assert S2_PRESET["session"] == "night"
    assert S2_PRESET["families"] == ("hist_momentum",)
    assert S2_PRESET["atr_rank_min"] == 0.7
    assert S2_PRESET["sl_atr"] == 3.0 and S2_PRESET["tp_atr"] == 4.5
    assert S2_PRESET["limit_off_atr"] == 0.3 and S2_PRESET["limit_wait"] == 2
    assert S2_SYMBOLS == ("EB888",)


def test_default_cfg_unpolluted():
    # 默认两族、白盘、无波动门、无限价偏移、TP=4.5（1.5R）
    assert DEFAULT_CFG["families"] == ("trend_pullback", "macd_cross")
    assert DEFAULT_CFG["session"] == (9, 15)
    assert DEFAULT_CFG["macd_slow"] == 27
    assert DEFAULT_CFG["atr_rank_min"] is None
    assert DEFAULT_CFG["limit_off_atr"] == 0.0
    assert DEFAULT_CFG["tp_atr"] == 4.5


def _ctx(n=120, ha_seq=None):
    """构造带指标的合成 ctx；dt 为夜盘 21:00 起每 5 分钟一根。"""
    base = pd.Timestamp("2026-11-20 21:00:00")
    dts = [base + pd.Timedelta(minutes=5 * i) for i in range(n)]
    close = 7000.0 + np.sin(np.arange(n) / 8.0) * 20.0
    if ha_seq is not None:
        # 反推 hist 让 hist_atr_raw ≈ ha_seq（ATR 稳定时用 close/ATR 无法精确控制，
        # 测试直接覆盖 hist_atr_raw 列以隔离信号层判定）。
        pass
    bars = pd.DataFrame({
        "dt": dts, "open": close, "high": close + 2, "low": close - 2,
        "close": close, "volume": np.ones(n) * 100,
    })
    daily = pd.DataFrame({"date": [pd.Timestamp("2026-11-19")],
                          "close": [6990.0]})
    return attach_indicators(bars, daily, S2_PRESET)


def test_s2_columns_causal():
    ctx = _ctx()
    for col in ("session", "hist_atr_raw", "atr_pct_rank", "daily_dir"):
        assert col in ctx.columns
    # 夜盘口径：hour>=20→night；21:00 起 120 根跨过午夜，前段 night、后段 am
    assert set(ctx["session"].unique()) == {"night", "am"}
    assert ctx["session"].iloc[0] == "night"
    # atr_pct_rank 前 59 根为 NaN（causal 50 根窗口），之后在 [0,1]
    assert np.isnan(ctx["atr_pct_rank"].to_numpy(float)[:59]).all()
    r = ctx["atr_pct_rank"].to_numpy(float)[59:]
    assert ((r >= 0) & (r <= 1)).all()


def test_hist_up010_fires_once_and_respects_gates():
    ctx = _ctx(n=120)
    ha = ctx["hist_atr_raw"].to_numpy(float).copy()
    # 造一次干净上穿：15 根前都 <=0.10，第 20 根跳到 0.20（仍为 23:40 night）
    ha[:20] = 0.05
    ha[20] = 0.20
    ha[21:] = 0.20
    ctx["hist_atr_raw"] = ha
    # 日线多 + 高波动全部满足
    ctx["daily_dir"] = 1
    ctx["atr_pct_rank"] = 0.9
    sigs = scan_signals(ctx, S2_PRESET)
    ups = [s for s in sigs if s.side == 1]
    assert len(ups) == 1, f"应只在上穿根出一次信号，实际 {[(s.index, s.side) for s in ups]}"
    assert ups[0].index == 20


def test_s2_gates_block_when_not_night_or_low_vol_or_short_daily():
    ctx = _ctx(n=120)
    ha = ctx["hist_atr_raw"].to_numpy(float).copy()
    ha[:20] = 0.05
    ha[20] = 0.20
    ctx["hist_atr_raw"] = ha
    ctx["daily_dir"] = 1
    ctx["atr_pct_rank"] = 0.9
    # 非夜盘 → 无信号
    ctx_night = ctx.copy()
    ctx_night["session"] = "am"
    assert [s for s in scan_signals(ctx_night, S2_PRESET) if s.side == 1] == []
    # 低波动 → 无信号
    ctx_lowvol = ctx.copy()
    ctx_lowvol["atr_pct_rank"] = 0.3
    assert [s for s in scan_signals(ctx_lowvol, S2_PRESET) if s.side == 1] == []
    # 日线空 → 多头信号被压住
    ctx_short = ctx.copy()
    ctx_short["daily_dir"] = -1
    assert [s for s in scan_signals(ctx_short, S2_PRESET) if s.side == 1] == []


def test_default_cfgs_do_not_produce_s2_signal():
    ctx = _ctx(n=120)
    ha = ctx["hist_atr_raw"].to_numpy(float).copy()
    ha[:20] = 0.05
    ha[20] = 0.20
    ctx["hist_atr_raw"] = ha
    ctx["daily_dir"] = 1
    ctx["atr_pct_rank"] = 0.9
    # 默认白盘两族路径不认识 hist_momentum，不应抛错也不应产生 S2 信号
    sigs = scan_signals(ctx, DEFAULT_CFG)
    assert all(s.family != "hist_momentum" for s in sigs)


def test_limit_offset_uses_pending_price_and_wait_window():
    """S2 限价：L=close-dir*0.3*ATR；wait 根内触及才成交，成交价=L。"""
    n = 30
    close = np.full(n, 100.0)
    # 制造真实波幅（Wilder ATR≈1.5），low 高于挂单价 L≈99.55 不触及
    high = np.full(n, 101.0)
    low = np.full(n, 99.8)
    bars = pd.DataFrame({"dt": [pd.Timestamp("2026-11-20 21:00:00")
                                + pd.Timedelta(minutes=5 * i) for i in range(n)],
                         "open": close, "high": high, "low": low,
                         "close": close, "volume": np.ones(n)})
    daily = pd.DataFrame({"date": [pd.Timestamp("2026-11-19")], "close": [100.0]})
    ctx = attach_indicators(bars, daily, S2_PRESET)
    sig_i = 15  # Wilder ATR14 需 >=14 根历史
    a = float(ctx["atr"].to_numpy(float)[sig_i])
    assert a > 0
    expected_L = 100.0 - 0.3 * a   # 多头挂下方 = 99.4
    assert low[0] > expected_L     # 默认 low 不触及挂单价
    sig = [Signal(sig_i, 1, 0.2, "hist_momentum")]
    # 第 1 根不触及（low 保持 99.5 > L），第 2 根触及 → wait=2 内成交
    ctx.loc[sig_i + 2, "low"] = expected_L - 0.5
    tr = run_replay(ctx, sig, "EB888", mult=5, tick=1, fee_per_side=3.0,
                    cfg=S2_PRESET, fill_mode="limit")
    assert len(tr) == 1
    assert abs(tr[0]["entry_px"] - expected_L) < 1e-6
    assert tr[0]["bars"] >= 1


def test_exit_plan_tp_is_1_5r():
    plan = ExitPlan(100.0, 1, 10.0, sl_atr=3.0, tp_atr=4.5, lot_size=1)
    assert abs(plan.r_unit - 30.0) < 1e-9
    assert abs(plan.target_px - 145.0) < 1e-9   # 100 + 4.5*10
    assert abs(plan.tp_r - 1.5) < 1e-9
    assert plan.to_exit_guard_payload()["tp1_r"] is None
