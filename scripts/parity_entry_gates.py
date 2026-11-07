#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对拍：entry_gates 模块（配置路径）vs validate_entry_gate.py 手工构造（研究路径）。

目的：确认生产接线（gates_for_symbol → walk_fusion_states）与已通过引擎级验证的
研究脚本 gate 构造**逐位一致**，影子观察才与验证结论同口径。

对拍项（3 品种 × 全历史小时线）：
  P1 gate 数组逐位一致（模块 vs 手工）
  P2 引擎 state 序列逐位一致（同 df 同 gates 双跑）
  P3 evaluate_symbol 配置路径（entry_gate_profile=final_v2）终态 == 手工 gates 终态
  P4 entry_gate_profile=off 时 evaluate_symbol 与旧路径终态一致（零影响）
"""
import sys
from pathlib import Path

# ★ 2026-11-06 修：原为 sys.path.insert(0, "/tmp/eg_run") —— 硬编码 Linux 临时路径。
#   在本仓（qhyc-dev）运行时，会把 /tmp/eg_run 的**旧副本**置于 import 路径之前
#   → 静默读到错误代码/数据（比直接报错更危险）；Windows 下该路径不存在直接失败。
#   改为按脚本位置推导仓库根，保证「跑的永远是本仓代码」。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.strategies.entry_gates import (
    gates_for_symbol, k45_daily_state, market_regime_series, K45, SMA_N,
    MID_LO, MID_HI,
)
from app.strategies.fusion_signal import (
    read_hourly_bars, drop_forming_bars, drop_unsettled, evaluate_symbol,
    fusion_state_detail, htf_direction, walk_fusion_states,
)

SYMS = ["FG888", "SA888", "SR888"]


def manual_gates(session, sym, hourly_df, symbols, k45_cache, reg_series):
    """逐行复刻 validate_entry_gate.py 的 gate 构造（研究路径，独立实现）。"""
    n = len(hourly_df)
    dts = hourly_df["dt"].to_numpy()
    st45 = k45_cache[sym]
    progs = np.full(n, np.nan)
    if st45 is not None:
        sdates = st45.index
        for i in range(n):
            d = pd.Timestamp(dts[i]).date()
            pos = sdates.searchsorted(pd.Timestamp(d), side="left")
            if pos > 0 and sdates[pos - 1].date() < d:
                progs[i] = st45.iloc[pos - 1]["prog"]
    mid = np.isfinite(progs) & (progs >= MID_LO) & (progs < MID_HI)
    gate_l = (~mid).astype(np.int8)
    gate_s = gate_l.copy()
    if reg_series is not None:
        reg_dates = reg_series.index.values
        for i in range(n):
            if mid[i]:
                continue
            d = pd.Timestamp(dts[i]).date()
            pos = np.searchsorted(reg_dates, np.datetime64(pd.Timestamp(d)), side="left")
            while pos > 0 and pd.Timestamp(reg_dates[pos - 1]).date() >= d:
                pos -= 1
            if pos <= 0:
                continue
            v = reg_series.iloc[pos - 1]
            if np.isfinite(v) and v > 0.5:
                gate_s[i] = 0
    return gate_l, gate_s


def main():
    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts]
    engine = create_engine(settings.db_url)
    session = sessionmaker(bind=engine)()

    # 手工路径的全量预计算（与 validate_entry_gate.py 相同的 SQL）
    rows = session.execute(text(
        "SELECT symbol, trade_date, high, low, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY symbol, trade_date"),
        {"syms": symbols}).all()
    ddf = pd.DataFrame(rows, columns=["symbol", "dt", "high", "low", "close"])
    ddf["dt"] = pd.to_datetime(ddf["dt"])
    k45_cache = {}
    for sym, g in ddf.groupby("symbol"):
        g = g.sort_values("dt").reset_index(drop=True)
        st = k45_daily_state(g["dt"].values,
                             g["high"].to_numpy(float),
                             g["low"].to_numpy(float),
                             g["close"].to_numpy(float)).shift(1)
        st.index = pd.to_datetime(st.index)
        k45_cache[sym] = st
    reg_series = market_regime_series(session, symbols)

    f_live = settings.fusion
    f_on = f_live.model_copy(update={"entry_gate_profile": "final_v2"})
    assert getattr(f_on, "entry_gate_profile") == "final_v2", "config 字段未生效"

    all_ok = True
    for sym in SYMS:
        # 生产口径 df（read_hourly_bars + drop_forming_bars + drop_unsettled，与 evaluate_symbol 一致）
        df = read_hourly_bars(session, sym, f_live.max_bars)
        df = drop_forming_bars(df)
        df = drop_unsettled(df, getattr(f_live, "settle_delay_sec", 0))
        if df is None or len(df) < f_live.min_bars:
            print(f"{sym}: bars 不足，跳过")
            continue
        gl_mod, gs_mod = gates_for_symbol(session, sym, df, symbols)
        gl_man, gs_man = manual_gates(session, sym, df, symbols, k45_cache, reg_series)
        p1 = np.array_equal(gl_mod, gl_man) and np.array_equal(gs_mod, gs_man)
        n_block_l = int((gl_mod == 0).sum())
        n_block_s = int((gs_mod == 0).sum())

        # P2：同 df 同 gates，引擎双跑（模块 gate vs 手工 gate）
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        htf = htf_direction(c, f_live.ema_k)
        s_mod = [d["state"] for d in walk_fusion_states(o, h, l, c, htf, f_live,
                                                         entry_gates=(gl_mod, gs_mod))]
        s_man = [d["state"] for d in walk_fusion_states(o, h, l, c, htf, f_live,
                                                         entry_gates=(gl_man, gs_man))]
        p2 = s_mod == s_man
        # 基线（无 gate）对比，看门控实际改变了多少根的 state
        s_base = [d["state"] for d in walk_fusion_states(o, h, l, c, htf, f_live)]
        n_state_diff = sum(1 for a, b in zip(s_base, s_mod) if a != b)

        # P3/P4：evaluate_symbol 配置路径
        r_off = evaluate_symbol(session, sym, f_live)
        r_on = evaluate_symbol(session, sym, f_on)
        det_man = fusion_state_detail(o, h, l, c, htf, f_live,
                                      symbol=sym,
                                      trade_date=pd.Timestamp(df["dt"].iloc[-1]).date(),
                                      entry_gates=(gl_man, gs_man))
        p3 = (r_on["state"] == det_man["state"]
              and r_on.get("entry_px") == det_man["entry_px"])
        det_off = fusion_state_detail(o, h, l, c, htf, f_live,
                                      symbol=sym,
                                      trade_date=pd.Timestamp(df["dt"].iloc[-1]).date())
        p4 = (r_off["state"] == det_off["state"]
              and r_off.get("entry_px") == det_off["entry_px"]
              and getattr(f_live, "entry_gate_profile", "off") == "off")

        ok = p1 and p2 and p3 and p4
        all_ok &= ok
        print(f"{sym}: P1(gate逐位)={p1} P2(state序列)={p2} P3(配置路径)={p3} "
              f"P4(off零影响)={p4} | bars={len(df)} 拦多={n_block_l} 拦空={n_block_s} "
              f"state差异根数={n_state_diff} | 终态 off={r_off['state']} on={r_on['state']}"
              f"{'' if ok else '  <-- FAIL'}")

    print("\nPARITY:", "ALL_OK" if all_ok else "MISMATCH")
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
