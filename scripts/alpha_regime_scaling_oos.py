#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regime 自适应减仓 —— WF-OOS 富集成交采集（2026-11-01，研究分支 cb/v2-oos-investigation）。

承接 §8.8 结论：2025–2026 失效是趋势捕获边在近期 regime 的系统性塌方，属结构衰减 old-school
alpha；主线应转向「regime 自适应减仓/空仓」或全新信号源。本脚本为「regime 自适应减仓」再研究
提供 WF-OOS 真实样本，并附加**市场级**（跨品种共享、按入场日取前一日、无前视）regime 特征：

  市场级（等权日指数 idx = ∏(1+跨品种均日收益)）：
    - reg_cont：idx / SMA250（连续比值，>1 多头排列）—— 前一交易日；
    - reg_bin ：idx > SMA250（1/0）—— 前一交易日；
    - mkt_adx ：对 idx 做 Wilder ADX(14)（趋势持续性，低=震荡）—— 前一交易日；
    - vol_pct  ：idx 20d 实现波动在滚动 250d 窗口内的分位（高=高波动/震荡 regime）—— 前一日；
    - mom      ：idx 60d 收益率（动量/趋势强度）—— 前一交易日。

  品种级（沿用 alpha_entry_filter_oos.py，供 per-asset 减仓对照）：
    - adx（60m 入场根）、atr_pctile（品种自身 ATR 滚动分位）、k45_dir/k45_prog（日线 K45 段）。

减仓评估在 alpha_regime_scaling_analyze.py 做：逐笔 net 是 per-unit R（对仓位尺度不变），
故减仓必须看**聚合 Σ(s_i·net_i)** 与**权益曲线（Sharpe/最大回撤）**，不能看逐笔均值。

无前视保证：所有特征取「entry date 的前一日收盘状态」，绝不引用未来。

用法（qhyc-api 容器内，PYTHONPATH 指向研究版 app 覆盖层）：
  python scripts/alpha_regime_scaling_oos.py [--max-symbols N] [--start 2021-01-01] \
      [--end 2026-10-30] [--out /tmp/alpha_regime_oos.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
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
    fusion_params_from_config, run_fusion_backtest,
)
from app.backtest.walkforward import WalkForwardParams, _windows, _dt

BARS_PER_DAY = 6.7
K45 = 45
SMA_N = 250
ADX_N = 14


def to_np_dt(arr):
    a = np.asarray(arr)
    if a.dtype.kind == "M":
        return a.astype("datetime64[ns]")
    return pd.to_datetime(arr).values.astype("datetime64[ns]")


# ------------------------------------------------------------------- 特征
def wilder_adx(h, l, c, n=ADX_N):
    """Wilder ADX(n)。h/l/c 可相等（对单一指数序列做趋势持续性）。"""
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    out = np.full(len(c), np.nan)
    if len(c) < n * 2 + 1:
        return out
    tr = np.zeros(len(c))
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    up = np.where(c[1:] > c[:-1], c[1:] - c[:-1], 0.0)
    dn = np.where(c[:-1] > c[1:], c[:-1] - c[1:], 0.0)
    tr_p = np.concatenate([[tr[0]], tr[1:]])
    pdm = np.concatenate([[0.0], up])
    mdm = np.concatenate([[0.0], dn])
    trs = np.zeros(len(c)); trs[n] = tr[1:n + 1].sum()
    for i in range(n + 1, len(c)):
        trs[i] = trs[i - 1] - trs[i - 1] / n + tr[i]
    pdms = np.zeros(len(c)); pdms[n] = pdm[1:n + 1].sum()
    mdms = np.zeros(len(c)); mdms[n] = mdm[1:n + 1].sum()
    for i in range(n + 1, len(c)):
        pdms[i] = pdms[i - 1] - pdms[i - 1] / n + pdm[i]
        mdms[i] = mdms[i - 1] - mdms[i - 1] / n + mdm[i]
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(trs > 0, 100 * pdms / trs, 0.0)
        mdi = np.where(trs > 0, 100 * mdms / trs, 0.0)
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx[~np.isfinite(dx)] = 0.0
    adx = np.full(len(c), np.nan)
    m = n * 2
    if len(c) > m:
        adx[m] = dx[n:m + 1].mean()
        for i in range(m + 1, len(c)):
            adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return adx


def build_market(session, symbols):
    """市场级等权日指数 + regime 特征（均 shift(1) 取前一日，无前视）。
    返回 (mkt_dates_np, dict[name->np.float array])。"""
    rows = session.execute(text(
        "SELECT symbol, trade_date, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY trade_date"), {"syms": symbols}).all()
    dd = pd.DataFrame(rows, columns=["symbol", "dt", "close"])
    dd["dt"] = pd.to_datetime(dd["dt"])
    wide = dd.pivot_table(index="dt", columns="symbol", values="close").sort_index()
    rets = wide.pct_change()
    idx = (1.0 + rets.mean(axis=1, skipna=True)).cumprod()
    sma = idx.rolling(SMA_N, min_periods=SMA_N).mean()
    reg_cont = (idx / sma).shift(1)
    reg_bin = (idx > sma).astype(float).shift(1)
    adx = pd.Series(wilder_adx(idx.to_numpy(float), idx.to_numpy(float),
                               idx.to_numpy(float)), index=idx.index).shift(1)
    r = idx.pct_change()
    vol20 = r.rolling(20, min_periods=10).std()
    # 滚动 250d 窗口内分位（当前 vol 在窗口里的位置）
    vol_pct = vol20.rolling(250, min_periods=50).apply(
        lambda x: ((x[-1] - x.min()) / (x.max() - x.min()))
        if (x.max() > x.min()) else 0.5, raw=True).shift(1)
    mom = idx.pct_change(60).shift(1)
    mkt_dates = to_np_dt(idx.index.values)
    out = {
        "reg_cont": reg_cont.to_numpy(float),
        "reg_bin": reg_bin.to_numpy(float),
        "mkt_adx": adx.to_numpy(float),
        "vol_pct": vol_pct.to_numpy(float),
        "mom": mom.to_numpy(float),
    }
    return mkt_dates, out


def k45_daily_state(dates, h, l, c, K=K45):
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
    piv = []
    for i, tp, px in cand:
        if piv and piv[-1][1] == tp:
            if (tp == "H" and px > piv[-1][2]) or (tp == "L" and px < piv[-1][2]):
                piv[-1] = (i, tp, px)
        elif piv:
            piv.append((i, tp, px))
        else:
            piv.append((i, tp, px))
    clean = []
    for p in piv:
        if clean and clean[-1][1] == p[1]:
            if (p[1] == "H" and p[2] > clean[-1][2]) or (p[1] == "L" and p[2] < clean[-1][2]):
                clean[-1] = p
        else:
            clean.append(p)
    piv = clean
    seg_dir = np.zeros(n, dtype=int)
    prog = np.full(n, np.nan)
    cur = None
    j = 0
    for t in range(n):
        while j < len(piv) and piv[j][0] + K <= t:
            cur = piv[j]; j += 1
        if cur is None:
            continue
        idx2, tp, px = cur
        if tp == "L":
            seg_dir[t] = 1
            runmax = h[idx2:t + 1].max()
            denom = runmax - px
            prog[t] = (c[t] - px) / denom if denom > 0 else np.nan
        else:
            seg_dir[t] = -1
            runmin = l[idx2:t + 1].min()
            denom = px - runmin
            prog[t] = (px - c[t]) / denom if denom > 0 else np.nan
    return pd.DataFrame({"seg_dir": seg_dir, "prog": prog}, index=dates)


def build_k45(session, symbols):
    drows = session.execute(text(
        "SELECT symbol, trade_date, high, low, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY symbol, trade_date"),
        {"syms": symbols}).all()
    ddf = pd.DataFrame(drows, columns=["symbol", "dt", "high", "low", "close"])
    ddf["dt"] = pd.to_datetime(ddf["dt"])
    cache = {}
    for sym, g in ddf.groupby("symbol"):
        g = g.sort_values("dt").reset_index(drop=True)
        st = k45_daily_state(g["dt"].values, g["high"].to_numpy(float),
                             g["low"].to_numpy(float), g["close"].to_numpy(float))
        st = st.shift(1)
        cache[sym] = (to_np_dt(st.index.values), st["seg_dir"].to_numpy(float),
                      st["prog"].to_numpy(float))
    return cache


def attach_features(trades, sym_60m, mkt, k45_cache):
    """附加市场级（按日，前一日）+ 品种级（日/60m）ex-ante 特征。纯 numpy searchsorted。"""
    mkt_dates = np.asarray(mkt[0], dtype="datetime64[ns]")
    mkt_f = {k: np.asarray(v, dtype=float) for k, v in mkt[1].items()}
    out = []
    for t in trades:
        sym = t.get("symbol") or ""
        ed = _dt(t.get("entry_dt"))
        if ed is None:
            continue
        rec = dict(t)
        rec["entry_date"] = ed.date().isoformat()
        ed_d = np.datetime64(ed.date())
        ed_ts = np.datetime64(ed)
        # 市场级（按日，前一日）
        mp = int(np.searchsorted(mkt_dates, ed_d, side="left"))
        for k in ("reg_cont", "reg_bin", "mkt_adx", "vol_pct", "mom"):
            rec[k] = None
            if mp > 0:
                v = mkt_f[k][mp - 1]
                if np.isfinite(v):
                    rec[k] = float(v)
        # 品种级 k45（日，前一日）
        rec["k45_dir"] = None
        rec["k45_prog"] = None
        kc = k45_cache.get(sym)
        if kc is not None:
            kdates, kdir, kprog = kc
            kp = int(np.searchsorted(kdates, ed_d, side="left"))
            if kp > 0:
                d = kdir[kp - 1]; p = kprog[kp - 1]
                if np.isfinite(d):
                    rec["k45_dir"] = int(d)
                if np.isfinite(p):
                    rec["k45_prog"] = float(p)
        # 品种级 60m adx / atr_pctile（入场根，前一根）
        rec["adx"] = None
        rec["atr_pctile"] = None
        sm = sym_60m.get(sym)
        if sm is not None:
            dts, adx, atr, atr_pct = sm
            ip = int(np.searchsorted(dts, ed_ts, side="left"))
            if ip > 0:
                a = adx[ip - 1]
                if np.isfinite(a):
                    rec["adx"] = float(a)
                q = atr_pct[ip - 1]
                if np.isfinite(q):
                    rec["atr_pctile"] = float(q)
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/alpha_regime_oos.json")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts] or []
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    p = fusion_params_from_config()

    print(f"[setup] symbols={len(symbols)} start={args.start} end={args.end}")

    # ---- 预计算特征 ----
    mkt = build_market(session, symbols)
    print(f"[market] 有效天 {int(np.isfinite(mkt[1]['mkt_adx']).sum())}")
    k45_cache = build_k45(session, symbols)
    print(f"[k45] {len(k45_cache)} symbols")

    from app.backtest.fusion_backtest import _read_hourly
    sym_60m = {}
    for si, sym in enumerate(symbols):
        df = _read_hourly(session, sym, "bar_60m", None, None)
        if df is None or len(df) < 300:
            continue
        h = df["high"].to_numpy(float); l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float); dts = df["dt"].to_numpy()
        adx = wilder_adx(h, l, c)
        rng = (h - l)
        atr = pd.Series(rng).rolling(60, min_periods=20).mean().to_numpy()
        atr_pct = np.full(len(c), np.nan)
        valid = np.isfinite(atr) & (atr > 0)
        if valid.any():
            win = pd.Series(rng).rolling(250, min_periods=50)
            lo = win.min().to_numpy(); hi = win.max().to_numpy()
            denom = hi - lo
            atr_pct = np.where((valid) & (denom > 0), (atr - lo) / denom, np.nan)
        sym_60m[sym] = (to_np_dt(dts), adx, atr, atr_pct)
        if (si + 1) % 10 == 0 or si == len(symbols) - 1:
            print(f"[60m] {si+1}/{len(symbols)} {sym}")

    # ---- WF-OOS 采集 ----
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    all_oos = []
    n_windows = 0
    for tr_s, tr_e, te_s, te_e in _windows(start, end, wfp):
        run_from = te_s - timedelta(days=wfp.warmup_days)
        res = run_fusion_backtest(session, symbols, p, run_from, te_e,
                                  include_trades=True)
        for t in (res.get("trades") or []):
            d = _dt(t.get("entry_dt"))
            if d is None:
                continue
            if d.date() >= te_s and not t.get("open"):
                t["symbol"] = t.get("symbol") or ""
                all_oos.append(t)
        n_windows += 1
    print(f"[wf] 窗口 {n_windows} 个；原始 OOS 已平仓成交 {len(all_oos)} 笔")

    enriched = attach_features(all_oos, sym_60m, mkt, k45_cache)
    session.close(); engine.dispose()
    print(f"[enrich] 富集成交 {len(enriched)} 笔")

    Path(args.out).write_text(
        json.dumps({"meta": {"symbols": len(symbols), "n_windows": n_windows,
                             "start": args.start, "end": args.end,
                             "bars_per_day": BARS_PER_DAY},
                    "rows": enriched},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"[dump] {args.out}  ({len(enriched)} rows)")


if __name__ == "__main__":
    main()
