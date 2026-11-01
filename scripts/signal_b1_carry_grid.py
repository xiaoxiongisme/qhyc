#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 B1 基差 carry 翻案：lag/方向/口径/调仓 网格 + 安慰剂（研究分支，未碰 main/cb/dev）。

对应 §12.7 P0。核心是**防前视稳健性**（纪律1）：探针证实 available_at==trade_date，
故信号显式 shift(lag)。若净边在 lag=1 有、lag=2/3 塌 -> 边靠「抢跑发布」= 前视假象；
若 lag↑ 边稳定 -> 慢变量本身有效（只是可能偏弱）。

网格：lag∈{1,2,3} × direction∈{+1 理论, -1 诊断} × value_kind∈{raw,z} × rebalance∈{5,21}。
  * direction=+1（贴水做多）是**经济先验固定的准入判定组**；
  * direction=-1 仅作**方向核验诊断**：若理论方向失败而反方向显著为正，如实记录
    「经济先验方向在本数据反了」，但**不作准入依据**（避免事后挑方向 = 选择偏差）。
  * 扫 24 格本身有多重比较假阳性，主判定只看理论先验固定格，其余为稳健性/诊断。

一次 load_market + 成本面预计算，循环 24 格，避免 24× 重查库（沿用 §10 编排）。

用法：
  python signal_b1_carry_grid.py --n-perm 0                          # 只跑 24 格网格（快）
  python signal_b1_carry_grid.py --only-perm --perm-grids "lag1/dir+1/raw/r5,lag2/dir+1/raw/r5" --n-perm 100
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, seg_stats, placebo_agg, SL_MULT)
from signal_b1_carry import load_carry_matrix, compute_targets

LAGS = (1, 2, 3)
DIRS = (1, -1)          # +1 理论（贴水做多） / -1 方向核验诊断
KINDS = ("raw", "z")
REBALS = (5, 21)
Q = 0.2
YEARS_GATE = ("2024", "2025", "2026")
YEARS = ("2022", "2023", "2024", "2025", "2026")


def key_of(lag, direction, kind, rb):
    d = "+1" if direction == 1 else "-1"
    return f"lag{lag}/dir{d}/{kind}/r{rb}"


def parse_key(key):
    lag_s, dir_s, kind, rb_s = key.split("/")
    return int(lag_s[3:]), (1 if dir_s == "dir+1" else -1), kind, int(rb_s[1:])


def build_targets(closes, symbols, raw, z, lag, direction, kind):
    t = compute_targets(closes, raw=raw, z=z, q=Q, lag=lag,
                        direction=direction, value_kind=kind, min_valid=10)
    return t.reindex(index=closes.index, columns=symbols).fillna(0.0)


def eval_grid(targets, opens, closes, atr, wfp, start, end, rb, contract_map, coef_cache):
    tr = simulate_windows(targets, opens, closes, atr, wfp, start, end,
                          rebalance_n=rb, sl_mult=SL_MULT,
                          contract_map=contract_map, coef_cache=coef_cache)
    o = seg_stats(tr)
    by_year = defaultdict(list)
    for r in tr:
        by_year[_dt(r["entry_dt"]).year].append(r)
    ys = {str(y): seg_stats(v) for y, v in sorted(by_year.items())}
    return o, ys


def gates(o, ys):
    """§6 前 3 门槛（gate4 安慰剂 / gate5 相关性另行）。"""
    net = o.get("mean_net_R") or 0
    pf = o.get("profit_factor") or 0
    g1 = net > 0
    g2 = isinstance(pf, (int, float)) and pf >= 1.1
    g3 = all((ys.get(y, {}).get("mean_net_R") or 0) > 0 for y in YEARS_GATE)
    return {"netR_pos": g1, "PF_ge_1_1": g2, "year24_26_all_pos": g3,
            "pass_123": g1 and g2 and g3}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--n-perm", type=int, default=0)
    ap.add_argument("--perm-grids", default="",
                    help="逗号分隔，格式 lag1/dir+1/raw/r5")
    ap.add_argument("--only-perm", action="store_true", help="只跑指定安慰剂格，跳过 24 格网格")
    ap.add_argument("--out", default="/tmp/aeq/b1_carry_grid.json")
    args = ap.parse_args()

    perm_set = {g.strip() for g in args.perm_grids.split(",") if g.strip()}

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    data_start = start - timedelta(days=400)
    print(f"[init] symbols={len(symbols)} start={start} end={end} "
          f"perm_grids={sorted(perm_set)} n_perm={args.n_perm} only_perm={args.only_perm}",
          flush=True)
    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    session.close(); engine.dispose()
    print(f"[load] n_dates={len(opens.index)}", flush=True)

    raw, z = load_carry_matrix(symbols, start, end)
    inter = sorted(set(symbols) & set(raw.columns))
    print(f"[carry] factor symbols={len(raw.columns)} 交集={len(inter)} "
          f"raw范围=[{raw.index.min().date()},{raw.index.max().date()}]", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    results = []
    if not args.only_perm:
        for lag in LAGS:
            for direction in DIRS:
                for kind in KINDS:
                    for rb in REBALS:
                        key = key_of(lag, direction, kind, rb)
                        tgt = build_targets(closes, symbols, raw, z, lag, direction, kind)
                        o, ys = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                                          rb, contract_map, coef_cache)
                        g = gates(o, ys)
                        yr = " ".join(f"{y}:{ys.get(y, {}).get('mean_net_R')}" for y in YEARS)
                        results.append({"grid": key, "lag": lag, "direction": direction,
                                        "value_kind": kind, "rebalance_n": rb,
                                        "overall": o, "by_year": ys, "gates": g})
                        tag = "理论" if direction == 1 else "诊断"
                        print(f"  {key:20s} [{tag}] n={o.get('n'):4d} 净R={o.get('mean_net_R')} "
                              f"PF={o.get('profit_factor')} 过123={g['pass_123']}  年[{yr}]",
                              flush=True)

    # ---- 安慰剂（指定格）----
    placebo = {}
    if args.n_perm > 0:
        for key in sorted(perm_set):
            lag, direction, kind, rb = parse_key(key)
            tgt = build_targets(closes, symbols, raw, z, lag, direction, kind)
            o, ys = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                              rb, contract_map, coef_cache)
            pct = placebo_agg(tgt, opens, closes, atr, wfp, start, end,
                              rb, SL_MULT, o["sum_net_R"], n_perm=args.n_perm,
                              contract_map=contract_map, coef_cache=coef_cache)
            placebo[key] = {"overall": o, "by_year": ys,
                            "rand_ge_actual_pct": pct, "n_perm": args.n_perm}
            print(f"[placebo] {key}: n={o.get('n')} 净R={o.get('mean_net_R')} "
                  f"PF={o.get('profit_factor')} rand_ge_actual%={pct}", flush=True)

    # ---- 稳健性汇总 ----
    summary = {}
    if results:
        for tag, dsel in (("theory_dir+1", 1), ("diagnostic_dir-1", -1)):
            grp = [r for r in results if r["direction"] == dsel]
            nets = [r["overall"].get("mean_net_R") or 0 for r in grp]
            pfs = [r["overall"].get("profit_factor") or 0 for r in grp]
            summary[tag] = {
                "n_grids": len(grp),
                "netR_pos_grids": sum(1 for v in nets if v > 0),
                "pf_ge_1_1_grids": sum(1 for v in pfs
                                       if isinstance(v, (int, float)) and v >= 1.1),
                "year24_26_all_pos_grids": sum(1 for r in grp if r["gates"]["year24_26_all_pos"]),
                "pass_123_grids": sum(1 for r in grp if r["gates"]["pass_123"]),
                "netR_median": round(float(np.median(nets)), 5),
                "netR_min": round(min(nets), 5), "netR_max": round(max(nets), 5),
            }
        # lag 稳健性（理论方向 + raw + r5）：lag↑ 时净R是否塌
        lag_rows = []
        for r in results:
            if r["direction"] == 1 and r["value_kind"] == "raw" and r["rebalance_n"] == 5:
                lag_rows.append({"lag": r["lag"], "mean_net_R": r["overall"].get("mean_net_R"),
                                 "PF": r["overall"].get("profit_factor"),
                                 "n": r["overall"].get("n")})
        lag_rows.sort(key=lambda x: x["lag"])
        summary["lag_stability_theory_raw_r5"] = lag_rows
        # 理论先验固定格
        prior = next((r for r in results if r["direction"] == 1 and r["lag"] == 1
                      and r["value_kind"] == "raw" and r["rebalance_n"] == 5), None)
        summary["theory_prior_fixed_cell"] = (
            {"grid": prior["grid"], "overall": prior["overall"],
             "by_year": prior["by_year"], "gates": prior["gates"]} if prior else None)

    report = {
        "meta": {"start": str(start), "end": str(end), "lags": list(LAGS),
                 "directions": list(DIRS), "value_kinds": list(KINDS),
                 "rebalances": list(REBALS), "q": Q,
                 "n_intersection": len(inter),
                 "available_at_note": "探针证实 available_at==trade_date(lag0)，禁用其对齐，"
                                      "改用显式 shift(lag) 滞后防前视",
                 "direction_note": "dir+1=经济先验(贴水做多)为准入判定组；"
                                   "dir-1=方向核验诊断，不作准入依据(避免事后挑方向)",
                 "multiple_comparison_note": "24 格存在多重比较假阳性，主判定只看理论先验固定格",
                 "n_windows": len(list(_windows(start, end, wfp)))},
        "grids": results, "summary": summary, "placebo": placebo,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print("\n=== SUMMARY ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    main()
