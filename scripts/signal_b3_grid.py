#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 参数敏感性网格 + 扩配对（研究分支 cb/v2-oos-investigation，未碰 main/cb/dev）。

对应 §9.7 待办1：验证 B3 正边是否对 window/entry_z 阈值脆弱、n=100 小样本是否偶然。

设计要点：
  * 一次 load_market + 预建主力合约成本面，循环 18 组 targets，避免 18× 重查库。
  * 网格：window∈{30,60,120} × entry_z∈{1.5,2.0,2.5} × pair_set∈{base8, base8+ext5}。
  * ext 配对池严格按「训练期(<=2023)日收益相关>=0.60 + 经济同板块 + 与 base 品种互斥」
    先验固定（scripts/_probe_b3_universe.py 探针佐证），**未窥探 2024-2026 OOS 表现**，
    杜绝「用回测表现挑对」的选择偏差。
  * 基准校验：base/60/2.0 必须逐位复现 §9 准入值（n=100, 净R≈+0.19063, PF≈1.861）。
  * 自检（纪律8）：ext 组 n 必须 > base 组 n，否则说明 pairs 参数静默退化，别怀疑市场。

用法：
  python signal_b3_grid.py --n-perm 0                      # 只跑 18 格网格（快）
  python signal_b3_grid.py --only-perm --perm-grids ext:60:2.0 --n-perm 100
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
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets

# 扩配对池（训练期日收益相关，pct_change(fill_method=None) 口径）：
#   FU-SC .778 / SF-SM .719 / C-CS .655 / CU-ZN .629 / NI-SS .604
# 均为经济同板块、且与 BASE 8 对（RB/HC/M/RM/Y/P/TA/MA/FG/SA/J/JM/PP/L/AU/AG）品种互斥
EXT_PAIRS = [("FU", "SC"), ("SF", "SM"), ("C", "CS"), ("CU", "ZN"), ("NI", "SS")]

WINDOWS = (30, 60, 120)
ENTRY_ZS = (1.5, 2.0, 2.5)
EXIT_Z = 0.5
YEARS = ("2022", "2023", "2024", "2025", "2026")


def build_targets(closes, symbols, pairs, window, entry_z):
    t = compute_targets(closes, pairs=pairs, window=window,
                        entry_z=entry_z, exit_z=EXIT_Z)
    return t.reindex(index=closes.index, columns=symbols).fillna(0.0)


def eval_grid(targets, opens, closes, atr, wfp, start, end, contract_map, coef_cache):
    tr = simulate_windows(targets, opens, closes, atr, wfp, start, end,
                          rebalance_n=1, sl_mult=SL_MULT,
                          contract_map=contract_map, coef_cache=coef_cache)
    o = seg_stats(tr)
    by_year = defaultdict(list)
    for r in tr:
        by_year[_dt(r["entry_dt"]).year].append(r)
    ys = {str(y): seg_stats(v) for y, v in sorted(by_year.items())}
    return o, ys, tr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--n-perm", type=int, default=0)
    ap.add_argument("--perm-grids", default="ext:60:2.0",
                    help="逗号分隔，格式 pairset:window:entry_z，如 ext:60:2.0,base:60:2.0")
    ap.add_argument("--only-perm", action="store_true", help="只跑指定安慰剂格，跳过 18 格网格")
    ap.add_argument("--out", default="/tmp/aeq/b3_grid.json")
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
    session.close()
    engine.dispose()
    print(f"[load] n_dates={len(opens.index)}", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    pair_sets = [("base", list(BASE_PAIRS)), ("ext", list(BASE_PAIRS) + EXT_PAIRS)]

    results = []
    if not args.only_perm:
        for ps, pairs in pair_sets:
            for w in WINDOWS:
                for ez in ENTRY_ZS:
                    key = f"{ps}:{w}:{ez}"
                    tgt = build_targets(closes, symbols, pairs, w, ez)
                    o, ys, tr = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                                          contract_map, coef_cache)
                    yr = " ".join(f"{y}:{ys.get(y, {}).get('mean_net_R')}" for y in YEARS)
                    results.append({"grid": key, "pair_set": ps, "n_pairs": len(pairs),
                                    "window": w, "entry_z": ez, "exit_z": EXIT_Z,
                                    "overall": o, "by_year": ys})
                    print(f"  {key:14s} n={o.get('n'):4d} 净R={o.get('mean_net_R')} "
                          f"PF={o.get('profit_factor')}  年[{yr}]", flush=True)

    # ---- 安慰剂（仅指定格）----
    placebo = {}
    if args.n_perm > 0:
        ps_map = dict(pair_sets)
        for key in sorted(perm_set):
            ps, w, ez = key.split(":")
            pairs = ps_map[ps]
            w, ez = int(w), float(ez)
            tgt = build_targets(closes, symbols, pairs, w, ez)
            o, ys, tr = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                                  contract_map, coef_cache)
            pct = placebo_agg(tgt, opens, closes, atr, wfp, start, end,
                              1, SL_MULT, o["sum_net_R"], n_perm=args.n_perm,
                              contract_map=contract_map, coef_cache=coef_cache)
            placebo[key] = {"overall": o,
                            "rand_ge_actual_pct": pct, "n_perm": args.n_perm}
            print(f"[placebo] {key}: n={o.get('n')} 净R={o.get('mean_net_R')} "
                  f"PF={o.get('profit_factor')} rand_ge_actual%={pct}", flush=True)

    # ---- 稳健性汇总 ----
    summary = {}
    if results:
        for ps in ("base", "ext"):
            grp = [r for r in results if r["pair_set"] == ps]
            nets = [r["overall"].get("mean_net_R") or 0 for r in grp]
            pfs = [r["overall"].get("profit_factor") or 0 for r in grp]
            ns = [r["overall"].get("n") or 0 for r in grp]
            allpos = sum(1 for r in grp if all(
                (r["by_year"].get(y, {}).get("mean_net_R") or 0) > 0
                for y in ("2024", "2025", "2026")))
            summary[ps] = {
                "n_grids": len(grp),
                "netR_pos_grids": sum(1 for v in nets if v > 0),
                "pf_ge_1_1_grids": sum(1 for v in pfs
                                       if isinstance(v, (int, float)) and v >= 1.1),
                "netR_min": round(min(nets), 5),
                "netR_median": round(float(np.median(nets)), 5),
                "netR_max": round(max(nets), 5),
                "n_trades_min": min(ns), "n_trades_max": max(ns),
                "year_2024_26_all_pos_grids": allpos,
            }
        # 逐格同参数比较（pair_set 不同、window/entry_z 相同），避免跨参数格误报
        cmp_rows = []
        for r in results:
            if r["pair_set"] != "base":
                continue
            er = next((x for x in results
                       if x["pair_set"] == "ext" and x["window"] == r["window"]
                       and x["entry_z"] == r["entry_z"]), None)
            if er:
                bn = r["overall"].get("n") or 0
                en = er["overall"].get("n") or 0
                cmp_rows.append({"grid": f"{r['window']}:{r['entry_z']}",
                                 "base_n": bn, "ext_n": en, "ext_gt_base": en > bn})
        n_ok = sum(1 for c in cmp_rows if c["ext_gt_base"])
        summary["ext_vs_base_per_grid"] = cmp_rows
        summary["ext_vs_base_sample_check"] = (
            f"OK: {n_ok}/{len(cmp_rows)} 同参数格 EXT n > BASE n（pairs 参数生效，样本量已拉开）"
            if cmp_rows and n_ok == len(cmp_rows)
            else "⚠ 存在 EXT n <= BASE n 的格：先查 pairs 参数是否静默退化，别怀疑市场")

    report = {
        "meta": {"start": str(start), "end": str(end), "windows": list(WINDOWS),
                 "entry_zs": list(ENTRY_ZS), "exit_z": EXIT_Z,
                 "base_pairs": [list(p) for p in BASE_PAIRS],
                 "ext_pairs": [list(p) for p in EXT_PAIRS],
                 "ext_pair_selection": "训练期(<=2023)日收益相关>=0.60 + 经济同板块 "
                                       "+ 与base品种互斥，未窥探2024-2026 OOS 表现",
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
