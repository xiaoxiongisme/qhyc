#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场门控 C_final_v2 引擎级验证（2026-11-01）：基线 vs 门控，同引擎同成本 A/B。

候选规则（信号级初筛：IS +25% / OOS +449%，安慰剂 300 次双侧 0%）：
  ① K45 中段停：日线 K=45 趋势进度 ∈ [33%, 50%) 时禁止一切新开仓（多空双侧）；
  ② 逆 regime 空头停：50 主连等权指数 > SMA250（前一交易日）时停发空头新开仓。
  两个状态全部用**前一交易日**收盘值（无前视）；只拦新开仓，离场/保本/吊灯/加码不动。

实现：walk_fusion_states 新增可选 entry_gates=(long_ok, short_ok)（默认 None
零影响）。本脚本对每品种预计算 gates，跑基线/门控两遍引擎，用
fusion_backtest._simulate_trades 复放成交（真实乘数 + 按建仓日历史费率）。

判据（全部满足才建议生产接入）：
  A. 门控 OOS 净利(元) > 0 且 > 基线 OOS 净利
  B. 门控 IS 净利 ≥ 基线 × 0.9（不深度损害样本内）
  C. 剔 TOP1/TOP3 贡献品种后门控 OOS 仍 > 基线
  D. 分年度：门控 OOS 各年恶化幅度不得出现"基线大赚年份被砍成大亏"

用法（容器内，PYTHONPATH 指向研究 worktree）：
  python scripts/validate_entry_gate.py [--out /tmp/eg_validate.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import (
    fusion_params_from_config, _read_hourly, _htf_direction, _atr,
    _simulate_trades,
)
from app.strategies.fusion_signal import walk_fusion_states

IS_END = pd.Timestamp("2021-01-01", tz="Asia/Shanghai")
K45 = 45
SMA_N = 250
MID_LO, MID_HI = 0.33, 0.50


def k45_daily_state(dates, h, l, c, K=K45):
    """与 entry_filter_study2.k45_daily_state 逐位一致（单一口径，改动须双改）。"""
    n = len(c)
    cand = []
    for i in range(n):
        lo, hi = max(0, i - K), min(n, i + K + 1)
        wh = h[lo:hi].max()
        if h[i] == wh and lo + int(np.argmax(h[lo:hi])) == i:
            cand.append((i, "H", float(h[i])))
        wl = l[lo:hi].min()
        if l[i] == wl and lo + int(np.argmin(l[lo:hi])) == i:
            cand.append((i, "L", float(l[i])))
    cand.sort(key=lambda x: x[0])
    clean = []
    for i, tp, px in cand:
        if clean and clean[-1][1] == tp:
            if (tp == "H" and px > clean[-1][2]) or (tp == "L" and px < clean[-1][2]):
                clean[-1] = (i, tp, px)
        else:
            clean.append((i, tp, px))
    seg_dir = np.zeros(n, dtype=int)
    prog = np.full(n, np.nan)
    cur = None
    j = 0
    for t in range(n):
        while j < len(clean) and clean[j][0] + K <= t:
            cur = clean[j]
            j += 1
        if cur is None:
            continue
        idx, tp, px = cur
        if tp == "L":
            seg_dir[t] = 1
            runmax = h[idx:t + 1].max()
            denom = runmax - px
            prog[t] = (c[t] - px) / denom if denom > 0 else np.nan
        else:
            seg_dir[t] = -1
            runmin = l[idx:t + 1].min()
            denom = px - runmin
            prog[t] = (px - c[t]) / denom if denom > 0 else np.nan
    return pd.DataFrame({"seg_dir": seg_dir, "prog": prog}, index=dates)


def build_states_df(df, p, gates=None):
    """消费 walk_fusion_states 构造 _simulate_trades 需要的 states_df。"""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    n = len(c)
    htf = _htf_direction(c, int(p.ema_k))
    states = np.zeros(n, dtype=int)
    lots_arr = np.zeros(n, dtype=int)
    for k, d in enumerate(walk_fusion_states(o, h, l, c, htf, p,
                                              entry_gates=gates)):
        states[k + 2] = d["state"]
        lots_arr[k + 2] = int(d.get("lots", 1) or 1)
    out = df.reset_index(drop=True).copy()
    out["state"] = states
    out["lots"] = lots_arr
    out["atr"] = _atr(out, getattr(p, "atr_n", 14)).to_numpy(dtype=float)
    return out


def agg(trades):
    closed = [t for t in trades if not t.get("open")]
    if not closed:
        return {"n": 0}
    pnl = np.array([t["pnl"] * t["mult"] for t in closed])  # 元
    rr = np.array([t["R"] for t in closed], dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    return {"n": int(len(closed)),
            "net_yuan": round(float(pnl.sum()), 0),
            "pf": round(pf, 4),
            "win_rate": round(float((pnl > 0).mean()) * 100, 2),
            "mean_R": round(float(rr.mean()), 5),
            "sum_R": round(float(rr.sum()), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/eg_validate.json")
    ap.add_argument("--profile", choices=("final_v2", "final_v3"), default="final_v2",
                    help="final_v3=final_v2 + 2021-2023 选出的五品种逆regime空头豁免")
    args = ap.parse_args()
    # D4 防过拟合名单：只用 2021-2023 逆regime空头净R排名选取，2024-2026 前向验证。
    REGIME_SHORT_WHITELIST = {"LH888", "LC888", "B888", "AG888", "L888"}

    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts] or []
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    p = fusion_params_from_config()

    # ---- 市场 regime（与 study2 同口径）----
    rows = session.execute(text(
        "SELECT symbol, trade_date, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY trade_date"), {"syms": symbols}).all()
    dd = pd.DataFrame(rows, columns=["symbol", "dt", "close"])
    dd["dt"] = pd.to_datetime(dd["dt"])
    wide = dd.pivot_table(index="dt", columns="symbol", values="close")
    rets = wide.pct_change(fill_method=None)
    idx_eq = (1.0 + rets.mean(axis=1, skipna=True)).cumprod()
    sma = idx_eq.rolling(SMA_N, min_periods=SMA_N).mean()
    regime_up = (idx_eq > sma).astype(float)
    regime_up[~(idx_eq.notna() & sma.notna())] = np.nan
    reg_series = pd.Series(regime_up.values, index=idx_eq.index).shift(1)
    reg_dates = idx_eq.index.values

    def _prev_regime_up(d):
        pos = np.searchsorted(reg_dates, np.datetime64(pd.Timestamp(d).date()), side="left")
        while pos > 0 and pd.Timestamp(reg_dates[pos - 1]).date() >= pd.Timestamp(d).date():
            pos -= 1
        if pos <= 0:
            return None
        v = reg_series.iloc[pos - 1]
        return None if not np.isfinite(v) else bool(v > 0.5)

    # ---- 每品种：K45 状态 + gates + 双跑 ----
    drows = session.execute(text(
        "SELECT symbol, trade_date, high, low, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY symbol, trade_date"),
        {"syms": symbols}).all()
    ddf = pd.DataFrame(drows, columns=["symbol", "dt", "high", "low", "close"])
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

    base_trades: list[dict] = []
    cand_trades: list[dict] = []
    for si, sym in enumerate(symbols):
        df = _read_hourly(session, sym, "bar_60m", None, None)
        if df is None or len(df) < 300:
            continue
        n = len(df)
        dts = df["dt"].to_numpy()
        st45 = k45_cache.get(sym)
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
        for i in range(n):
            if mid[i]:
                continue
            ru = _prev_regime_up(pd.Timestamp(dts[i]))
            if ru and not (args.profile == "final_v3" and sym in REGIME_SHORT_WHITELIST):
                gate_s[i] = 0
        sdf_base = build_states_df(df, p, gates=None)
        sdf_cand = build_states_df(df, p, gates=(gate_l, gate_s))
        tb = _simulate_trades(sdf_base, p, symbol=sym)
        tc = _simulate_trades(sdf_cand, p, symbol=sym)
        for t in tb:
            t["symbol"] = sym
        for t in tc:
            t["symbol"] = sym
        base_trades.extend(tb)
        cand_trades.extend(tc)
        if (si + 1) % 10 == 0 or si == len(symbols) - 1:
            print(f"[{si+1}/{len(symbols)}] {sym}: base={len(tb)} cand={len(tc)}")

    session.close()
    engine.dispose()

    def split(trades):
        isx = [t for t in trades if pd.Timestamp(t["edt"]) < IS_END]
        oos = [t for t in trades if pd.Timestamp(t["edt"]) >= IS_END]
        return isx, oos

    def per_symbol_net(trades):
        out = {}
        for t in trades:
            out[t["symbol"]] = out.get(t["symbol"], 0.0) + t["pnl"] * t["mult"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def ex_top(trades, k):
        nets = per_symbol_net(trades)
        drop = set(list(nets)[:k])
        return [t for t in trades if t["symbol"] not in drop]

    bi, bo = split(base_trades)
    ci, co = split(cand_trades)
    report = {
        "profile": args.profile,
        "regime_short_whitelist": (sorted(REGIME_SHORT_WHITELIST)
                                   if args.profile == "final_v3" else []),
        "base": {"ALL": agg(base_trades), "IS": agg(bi), "OOS": agg(bo)},
        "cand": {"ALL": agg(cand_trades), "IS": agg(ci), "OOS": agg(co)},
    }
    report["base"]["OOS_exTOP1"] = agg(ex_top(bo, 1))
    report["base"]["OOS_exTOP3"] = agg(ex_top(bo, 3))
    report["cand"]["OOS_exTOP1"] = agg(ex_top(co, 1))
    report["cand"]["OOS_exTOP3"] = agg(ex_top(co, 3))
    # 分年度
    for tag, tr in (("base", base_trades), ("cand", cand_trades)):
        yr = {}
        for t in tr:
            if pd.Timestamp(t["edt"]) >= IS_END:
                y = pd.Timestamp(t["edt"]).year
                yr.setdefault(y, []).append(t)
        report[tag]["OOS_by_year"] = {y: agg(v) for y, v in sorted(yr.items())}
    report["per_symbol_net"] = {"base": per_symbol_net(bo),
                                "cand": per_symbol_net(co)}

    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in report[k].items()
                          if kk in ("ALL", "IS", "OOS", "OOS_exTOP1", "OOS_exTOP3")}
                      for k in ("base", "cand")}, ensure_ascii=False, indent=1))
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
