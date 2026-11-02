#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三期预研：权重方向「IC 为正 vs 组合净R 为负」分层口径对账（研究分支，§17）。

矛盾
----
§17 IC 核验（signal_weight_sign_ic.py）：cross_rank_z/member_ls_z/member_net_z 三个
正权重因子，OOS 横截面 IC 显著为正（t>3），方向与 default_weight 一致 → 「平台符号反转」不成立。
§15 P1 组合回测：同样这三个，权重方向净 R 全负（cross_rank -0.10、member_net -0.198），
反方向 anti 反而为正 → 当时疑似「符号反转」。

同一批成交、同一个方向，两套口径给出相反符号。本脚本在**完全相同的成交**上逐笔重算，
把「口径」这一层单独剥离出来，定位矛盾究竟出在哪一层。四套口径逐层递进：

  [K1] 原始 mean_net_R   = (pnl_points - cost_points)/(3×ATR)   ← P1/§15 用的口径（open-to-open）
  [K2] 原始 mean_gross_R = pnl_points/(3×ATR)                    ← 剥离成本，看是不是成本吃光
  [K3] 归一收益率         = (pnl_points - cost_points)/entry_px   ← 剥离 ATR 归一（与 IC 的收益率口径对齐）
  [K4] IC 对齐口径组合收益 = Σ target·(close[d+1]/close[d]-1)/n  ← 与 IC 完全同源（close-to-close、离散仓位）

判读逻辑：
  - K1 负、K2 仍负        → 不是成本问题（成本 R 量级 ~0.014，吃不掉 -0.1~-0.2）
  - K2 负、K3 转正        → ATR 归一是元凶（§11 已见 RB-HC sumR+0.41 但归一收益-0.025）
  - K3 仍负、K4 正        → open-to-open 成交时点/隔夜跳空是元凶
  - K4 也负               → 离散 top/bottom 20% 仓位构造与 z 的连续秩相关脱节（极端分位失效）

另对 K3 做反方向对照、逐年分解，确认不是单年撑起。所有变体复用 §9 底座原始成交，不改 simulate。

用法：
  python signal_weight_sign_recon.py --out /tmp/aeq/p3_weight_sign_recon.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import WalkForwardParams, _windows, _dt
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, seg_stats, SL_MULT)
from signal_factor_eval import load_factor_matrices, build_targets, SIGN

# 三个 IC-OOS 显著为正的正权重因子（来自 p3_weight_sign.json）
FOCUS = ["cross_rank_z", "member_ls_z", "member_net_z"]
LAGS = (1, 2, 3)


def amount_stats(trades):
    """K3：归一收益率口径（pnl - cost)/entry_px，跨品种可比。"""
    if not trades:
        return {"n": 0, "sum_net_return": None, "mean_net_return": None, "pos_ratio": None}
    ret = np.array([(t["pnl"] - t["cost_pts"]) / t["entry_px"] for t in trades
                    if t["entry_px"] and t["entry_px"] == t["entry_px"]], dtype=float)
    gross = np.array([t["pnl"] / t["entry_px"] for t in trades
                      if t["entry_px"] and t["entry_px"] == t["entry_px"]], dtype=float)
    return {
        "n": int(len(ret)),
        "sum_net_return": round(float(ret.sum()), 5),
        "mean_net_return": round(float(ret.mean()), 5),
        "mean_gross_return": round(float(gross.mean()), 5),
        "pos_ratio": round(float((ret > 0).mean()), 4),
    }


def ic_aligned_return(closes, targets, lag=1):
    """K4：与 IC 完全同源的 close-to-close 离散仓位组合收益。

    用与 P1 相同的离散 target（top-q 做多/bottom-q 做空），但改用
    close[d+1]/close[d]-1 收益率，与 signal_weight_sign_ic.py 的 fwd 口径一致。
    targets 已 shift(lag)；这里用 targets.loc[d] 对齐 close[d+1]/close[d]-1 的 d 行。
    """
    cols = targets.columns.intersection(closes.columns)
    c = closes[cols].astype(float)
    fwd = c.shift(-1) / c - 1.0
    tg = targets[cols]
    recs = {}
    for d in tg.index:
        t = tg.loc[d].to_numpy(dtype=float)
        r = fwd.loc[d].to_numpy(dtype=float)
        m = np.isfinite(t) & np.isfinite(r) & (t != 0)
        if m.sum() < 5:
            continue
        w = t[m] / np.abs(t[m]).sum()          # 归一化到名义 1（多空各半）
        recs[pd.Timestamp(d).date()] = float(np.dot(w, r[m]))
    return pd.Series(recs).sort_index()


def _series_stat(s):
    v = np.asarray(s, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 3:
        return {"n": len(v), "mean": None, "t": None}
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    t = mu / (sd / np.sqrt(len(v))) if sd > 0 else None
    return {"n": len(v), "mean": round(mu, 6), "t": round(t, 3) if t is not None else None}


def horizon_scan(opens, closes, targets, horizons=(1, 3, 5, 10, 20), oos_start=None):
    """离散仓位、多持有期、双价格基准的组合收益（纯矩阵，与 IC 同源但可扫持有期）。

    close 基准：Σ w·(close[d+h]/close[d]-1)；open 基准：Σ w·(open[d+h]/open[d]-1)。
    用同一 target（离散 top/bottom），只切换「价格基准」与「持有期」两个变量，
    用于分离 open-to-open 成交时点 vs 持有期效应，定位 K1-3 负、IC 正的矛盾来源。
    """
    cols = targets.columns.intersection(closes.columns)
    tg = targets[cols]
    out = {"close": {}, "open": {}}
    for kind, px in (("close", closes[cols].astype(float)),
                     ("open", opens[cols].astype(float))):
        for h in horizons:
            fwd = px.shift(-h) / px - 1.0
            recs = {}
            for d in tg.index:
                t = tg.loc[d].to_numpy(dtype=float)
                r = fwd.loc[d].to_numpy(dtype=float)
                m = np.isfinite(t) & np.isfinite(r) & (t != 0)
                if m.sum() < 5:
                    continue
                w = t[m] / np.abs(t[m]).sum()
                recs[pd.Timestamp(d).date()] = float(np.dot(w, r[m]))
            s = pd.Series(recs).sort_index()
            if oos_start is not None:
                s = s[s.index >= oos_start]
            out[kind][str(h)] = _series_stat(s.to_numpy())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--q", type=float, default=0.2)
    ap.add_argument("--rebalance-n", type=int, default=5)
    ap.add_argument("--oos-start", default="2024-01-01")
    ap.add_argument("--out", default="/tmp/aeq/p3_weight_sign_recon.json")
    args = ap.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    oos_start = date.fromisoformat(args.oos_start)
    data_start = start - timedelta(days=400)

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    print(f"[init] focus={FOCUS} lags={LAGS} rebalance_n={args.rebalance_n} q={args.q}", flush=True)
    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    session.close(); engine.dispose()
    print(f"[load] n_dates={len(opens.index)}", flush=True)

    mats = load_factor_matrices(FOCUS, start, end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    rows = []
    for fid in FOCUS:
        mat = mats[fid]["z"]
        for mode, sign in (("weight", SIGN[fid]), ("anti", -SIGN[fid])):
            for lag in LAGS:
                tgt = build_targets(closes, mat, args.q, lag, sign)
                tr = simulate_windows(tgt, opens, closes, atr, wfp, start, end,
                                      rebalance_n=args.rebalance_n, sl_mult=SL_MULT,
                                      contract_map=contract_map, coef_cache=coef_cache)
                o = seg_stats(tr)
                am = amount_stats(tr)
                # K4：IC 对齐口径（close-to-close），逐年 + OOS
                icr = ic_aligned_return(closes, tgt, lag=lag)
                is_mask = icr.index < oos_start
                k4_full = _series_stat(icr.to_numpy())
                k4_oos = _series_stat(icr[~is_mask].to_numpy())
                # K5/K6：双价格基准 × 多持有期扫描（纯矩阵），分离成交时点 vs 持有期
                hs = horizon_scan(opens, closes, tgt, horizons=(1, 3, 5, 10, 20),
                                  oos_start=oos_start)
                # K3 逐年（按 entry_dt）
                by_year = defaultdict(list)
                for t in tr:
                    by_year[_dt(t["entry_dt"]).year].append(t)
                k3_year = {str(y): amount_stats(v)["sum_net_return"]
                           for y, v in sorted(by_year.items())}
                rec = {
                    "factor_id": fid, "mode": mode, "sign": sign, "lag": lag,
                    "K1_mean_net_R": o.get("mean_net_R"),
                    "K2_mean_gross_R": o.get("mean_gross_R"),
                    "K1_mean_cost_R": o.get("mean_cost_R"),
                    "K1_PF": o.get("profit_factor"), "n_trades": o.get("n"),
                    "K3_net_return": am["sum_net_return"],
                    "K3_mean_return": am["mean_net_return"],
                    "K3_mean_gross_return": am["mean_gross_return"],
                    "K3_by_year": k3_year,
                    "K4_ic_aligned_full": k4_full,
                    "K4_ic_aligned_oos": k4_oos,
                    "K5_close_by_h": hs["close"],
                    "K6_open_by_h": hs["open"],
                }
                rows.append(rec)
                print(f"  {fid:20s} {mode:6s} lag{lag}: K1netR={o.get('mean_net_R')} "
                      f"K2grossR={o.get('mean_gross_R')} K3净收益={am['sum_net_return']} "
                      f"K4_oos={k4_oos['mean']}(t={k4_oos['t']})", flush=True)

    # 定位判读（数据驱动，给数值不武断）
    def judge(r):
        k1, k2, k3 = r["K1_mean_net_R"], r["K2_mean_gross_R"], r["K3_net_return"]
        parts = []
        parts.append("K2gross仍负→非成本" if (k2 is not None and k2 < 0) else "K2gross≥0→成本可解释")
        parts.append("K3归一收益仍负→ATR归一无辜(§11解释在此不成立)"
                     if (k3 is not None and k3 < 0) else "K3转正→ATR归一翻转符号")
        c1 = (r["K5_close_by_h"].get("1") or {}).get("mean")
        o1 = (r["K6_open_by_h"].get("1") or {}).get("mean")
        c20 = (r["K5_close_by_h"].get("20") or {}).get("mean")
        o20 = (r["K6_open_by_h"].get("20") or {}).get("mean")
        if c1 is not None and o1 is not None:
            parts.append(f"离散组合 close1={c1:+.4f}/open1={o1:+.4f}")
        if c20 is not None and o20 is not None:
            parts.append(f"close20={c20:+.4f}/open20={o20:+.4f}")
        # 只在证据充分时下归因，否则标注"证据不足"
        if c1 is not None and c1 > 0 and o1 is not None and o1 < 0:
            parts.append("→close口径正而open口径负：成交时点(隔夜跳空)令方向反转")
        elif c20 is not None and c20 > 0 and k3 is not None and k3 < 0:
            parts.append("→close长持有为正而open-to-open持仓为负：持有期/成交结构背离")
        else:
            parts.append("→组合层面方向弱或不显著：不足以翻案，维持§15无准入")
        return "；".join(parts)
    for r in rows:
        r["diagnosis"] = judge(r)

    print("\n======== 分层口径对账（权重方向主判据）========", flush=True)
    print("%-20s %4s %9s %9s %11s | %10s %10s %10s %10s" %
          ("factor", "lag", "K1netR", "K2grossR", "K3净收益",
           "close1", "open1", "close20", "open20"), flush=True)
    for r in rows:
        if r["mode"] != "weight":
            continue
        c = r["K5_close_by_h"]; o = r["K6_open_by_h"]
        print("%-20s %4d %9s %9s %11s | %10s %10s %10s %10s" %
              (r["factor_id"], r["lag"], r["K1_mean_net_R"], r["K2_mean_gross_R"],
               r["K3_net_return"],
               (c.get("1") or {}).get("mean"), (o.get("1") or {}).get("mean"),
               (c.get("20") or {}).get("mean"), (o.get("20") or {}).get("mean")), flush=True)
    print("\n======== 诊断（权重方向 lag1）========", flush=True)
    for r in rows:
        if r["mode"] == "weight" and r["lag"] == 1:
            print(f"  {r['factor_id']}: {r['diagnosis']}", flush=True)

    report = {
        "meta": {"start": str(start), "end": str(end), "focus": FOCUS, "lags": list(LAGS),
                 "q": args.q, "rebalance_n": args.rebalance_n, "oos_start": str(oos_start),
                 "K1": "mean_net_R=(pnl-cost)/(3×ATR) open-to-open 含成本 [P1口径]",
                 "K2": "mean_gross_R=pnl/(3×ATR) open-to-open 零成本",
                 "K3": "(pnl-cost)/entry_px 归一收益率 open-to-open 含成本 [与IC收益率口径对齐]",
                 "K4": "Σtarget·(close[d+1]/close[d]-1)/n 离散仓位 close-to-close [与IC完全同源]",
                 "judge_guide": "K2<0非成本→看K3是否转正(ATR归一)→看K4是否正(open成交跳空)→否则离散仓位脱节"},
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
