#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Alpha 入场质量过滤 —— WF-OOS 富集成交采集（2026-11-01，研究分支 cb/v2-oos-investigation）。

承接 docs/V2样本外崩塌排查.md §8.5 #2(c) 与 §8.7.7 #4：
  entry_filter_study2.py 已用 regime/K45/ADX 类过滤 + 安慰剂对照，但用的是「信号级连续重放
  （entry_dt>=2021 即算 OOS，无 walk-forward 窗口/warmup）」且只算「毛 R（无真实成本/乘数/加码）」。
  本脚本把同样的 ex-ante 特征挪到 alpha_entry_quality.py 那种**严格 WF-OOS + 真实净 R** 口径上，
  为「WF 真实盈亏重校验」提供样本。

做法：
  ① 复用 alpha_entry_quality.py 的 WF-OOS 采集骨架（train720/test60/step60/warmup150，
     run_fusion_backtest(include_trades=True) —— 真实乘数/真实历史费率成本/加码），
     产出逐笔 毛R/成本R/净R/side/entry_reason/exit_reason/holding_bars；
  ② 对每笔入场（entry_dt）附加无前视的 ex-ante 特征：
       - regime：50 主连等权日收益指数 vs SMA250（前一交易日），up=1/down=0；
       - k45_dir / k45_prog：每品种日线 K45 摆动段（前后各 45 根极值）方向/进度（前一日）；
       - adx：bar_60m Wilder ADX(14) 入场根（策略已有 adx_min=15，此处复核+测试更高阈值）；
       - atr_pctile：入场 ATR 在品种自身滚动分位（波动率 regime）。
  ③ 按 (symbol, entry_dt.date()) 关联，dump 富集成交 JSON，供本地 alpha_entry_filter_analyze.py
     做门过滤 + 安慰剂 + 分年稳定性 + 可修复/已衰减判定。

无前视保证：所有特征取「entry bar / entry date 的前一日收盘状态」，绝不引用未来。

用法（qhyc-api 容器内，PYTHONPATH 指向研究版 app 覆盖层）：
  python scripts/alpha_entry_filter_oos.py [--max-symbols N] [--start 2021-01-01] \
      [--end 2026-10-30] [--out /tmp/alpha_filter_oos.json]
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
    """统一转 np.datetime64[ns]；源可能是 datetime64 或对象/字符串。"""
    a = np.asarray(arr)
    if a.dtype.kind == "M":
        return a.astype("datetime64[ns]")
    return pd.to_datetime(arr).values.astype("datetime64[ns]")


# ------------------------------------------------------------------- 特征
def wilder_adx(h, l, c, n=ADX_N):
    """Wilder ADX(n) on 60m. 返回与输入等长数组（前 n*2 为 NaN）。"""
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
    # Wilder smoothing
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


def k45_daily_state(dates, h, l, c, K=K45):
    """日线 K45 摆动段：返回 DataFrame(index=dates)：seg_dir(+1/-1/0), prog(NaN=无段)。
    无前视：枢轴 i 在 i+K 才确认；t 日状态 = 最后一个 confirm_idx<=t 的枢轴之后的段。
    """
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


def build_regime(session, symbols):
    """市场级等权日收益指数 vs SMA250（前一日）。返回 (reg_dates, reg_prev) 用于按日查询。"""
    rows = session.execute(text(
        "SELECT symbol, trade_date, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY trade_date"), {"syms": symbols}).all()
    dd = pd.DataFrame(rows, columns=["symbol", "dt", "close"])
    dd["dt"] = pd.to_datetime(dd["dt"])
    wide = dd.pivot_table(index="dt", columns="symbol", values="close")
    rets = wide.pct_change()
    idx_eq = (1.0 + rets.mean(axis=1, skipna=True)).cumprod()
    sma = idx_eq.rolling(SMA_N, min_periods=SMA_N).mean()
    regime_up = (idx_eq > sma)
    ser = pd.Series(regime_up.values.astype(float), index=idx_eq.index).shift(1)
    reg_dates = idx_eq.index.values
    reg_prev = ser.values.astype(float)
    return reg_dates, reg_prev


def build_k45(session, symbols):
    """每品种 K45 日线状态（前一日），返回 {symbol: (dates, seg_dir, prog)}。"""
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


def attach_features(trades, sym_60m, regime, k45_cache):
    """对采集到的成交附加 ex-ante 特征。纯 numpy searchsorted（无 per-trade pandas 转换）。

    sym_60m: {symbol: (dts_np, adx, atr, atr_pctile)}；k45_cache: {symbol: (dates_np, dir, prog)}；
    regime: (dates_np, prev_float)。所有日期数组已在 main 预转为 datetime64[ns]。
    """
    reg_dates, reg_prev = regime
    reg_dates = np.asarray(reg_dates, dtype="datetime64[ns]")
    reg_prev = np.asarray(reg_prev, dtype=float)
    out = []
    for t in trades:
        sym = t.get("symbol") or ""
        ed = _dt(t.get("entry_dt"))
        if ed is None:
            continue
        rec = dict(t)
        rec["entry_date"] = ed.date().isoformat()
        ed_d = np.datetime64(ed.date())          # 日级（regime / k45 用前一日）
        ed_ts = np.datetime64(ed)                # 60m 根级（adx 用）
        # regime（市场级，按日，前一日）
        rec["reg"] = None
        rp = int(np.searchsorted(reg_dates, ed_d, side="left"))
        if rp > 0:
            pv = reg_prev[rp - 1]
            if np.isfinite(pv):
                rec["reg"] = float(pv)
        # k45（品种级，前一日）
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
        # adx / atr_pctile（60m 入场根）
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
    ap.add_argument("--out", default="/tmp/alpha_filter_oos.json")
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

    # ---- 预计算 ex-ante 特征 ----
    regime = build_regime(session, symbols)
    print(f"[regime] 有效 {int(np.isfinite(regime[1]).sum())} 天")
    k45_cache = build_k45(session, symbols)
    print(f"[k45] {len(k45_cache)} symbols")

    # 60m ADX + ATR 分位（每品种一次）
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

    # ---- WF-OOS 采集（与 alpha_entry_quality.py 同口径）----
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

    # ---- 附加特征 ----
    enriched = attach_features(all_oos, sym_60m, regime, k45_cache)
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
