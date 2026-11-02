#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六期 P1：修正日历口径后重跑 B3，验证「单腿裸露」是否消失、既有结论是否翻案（§26）。

P0 实证推翻「FU888 缺 15.2% 行情」：331 个缺失日中 330 个是周六，系
SC/AG/AU 等夜盘跨到周六凌晨的 60m bar 被 .dt.floor("D") 记成「周六」、
而日历取品种并集所致；l3_ref.trade_calendar 只认周一至周五；剔周末后
FU888 真实交易日覆盖率 99.95%。故本脚本不「补 bar」，改为三口径日历并排：

  raw          —— 原口径（基线，须与 §9/§10/§19/§25.2 逐位一致：n=195、口径A 9 组单腿）
  weekend_drop —— 剔除周六/周日 bar（保守简化，丢弃夜盘跨日行情）
  session_map  —— 夜盘归属下一交易日（hour>=21 归次日；周末推到下周一）

对比维度：成交数、净R/PF、口径A 单腿组、口径B 贪心真单腿、归一收益率资金曲线、
2026 MDD、分年、逐对归一收益、FU-SC 成交笔数。判定单腿是否消失、§6 五门槛是否翻案。

用法：python signal_b3_calendar.py --out /tmp/aeq/b3_calendar.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, seg_stats, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS
from signal_b3_capacity import pair_of
from signal_b3_equity import equity_curve, by_year_equity, _d

PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS
MODES = ("raw", "weekend_drop", "session_map")


def _dd(x):
    return date.fromisoformat(_d(x))


def naked_grouping(trades, pairs, max_gap=7):
    """口径A：(entry_dt,pair) 分组；口径B：贪心邻近配对（同对、反向、<=max_gap 日）。"""
    gA = defaultdict(list)
    for tr in trades:
        gA[(_d(tr["entry_dt"]), pair_of(tr["symbol"], pairs))].append(tr)
    nakedA = {k: v for k, v in gA.items() if len(v) != 2}

    bypair = defaultdict(list)
    for tr in trades:
        bypair[pair_of(tr["symbol"], pairs)].append(tr)
    paired, true_naked = 0, []
    for p, ts in bypair.items():
        ts = sorted(ts, key=lambda x: _dd(x["entry_dt"]))
        used = [False] * len(ts)
        for i, tr in enumerate(ts):
            if used[i]:
                continue
            best, bestd = None, 10 ** 9
            for j in range(i + 1, len(ts)):
                if used[j] or ts[j]["side"] == tr["side"]:
                    continue
                gap = (_dd(ts[j]["entry_dt"]) - _dd(tr["entry_dt"])).days
                if 0 <= gap <= max_gap and gap < bestd:
                    bestd, best = gap, j
            if best is not None:
                used[i] = used[best] = True
                paired += 1
            else:
                true_naked.append(tr)
    return {
        "A_naked_groups": len(nakedA),
        "A_naked_by_pair": dict(defaultdict(int, {
            p: sum(1 for (d, pp) in nakedA if pp == p) for p in {pp for _, pp in nakedA}
        })),
        "A_naked_detail": [
            {"entry": k[0], "pair": k[1],
             "legs": [{"symbol": x["symbol"], "side": x["side"],
                       "pnl": round(float(x["pnl"]), 1)} for x in v]}
            for k, v in sorted(nakedA.items())],
        "B_n_paired": paired,
        "B_true_naked": len(true_naked),
        "B_true_naked_detail": [
            {"entry": _d(x["entry_dt"]), "symbol": x["symbol"], "side": x["side"],
             "pair": pair_of(x["symbol"], pairs)} for x in true_naked],
    }


def analyze(trades, calendar, pairs, max_gap=7):
    base = seg_stats(trades)
    ec = equity_curve(trades, calendar, denom="return")
    by_year = by_year_equity(trades, calendar, denom="return")
    cal26 = [d for d in calendar if str(d)[:4] == "2026"]
    t26 = [x for x in trades if _d(x["entry_dt"])[:4] == "2026"]
    ec26 = equity_curve(t26, cal26, denom="return") if t26 else {"n": 0}

    bp = defaultdict(list)
    for tr in trades:
        bp[pair_of(tr["symbol"], pairs)].append(tr)
    per_pair = {}
    for p, ts in bp.items():
        s = sum((x["pnl"] - x["cost_pts"]) / x["entry_px"]
                for x in ts if x["entry_px"])
        per_pair[p] = {"n": len(ts), "sum_return_px": round(s, 5)}

    gates = {
        "net_R_positive": (base.get("mean_net_R") or 0) > 0,
        "PF_ge_1_1": (base.get("profit_factor") if isinstance(
            base.get("profit_factor"), (int, float)) else 0) >= 1.1,
        "no_year_crash_2024_2026": all(
            (by_year.get(y, {}).get("n", 0) == 0 or
             (by_year.get(y, {}).get("total_return") or 0) > 0)
            for y in ("2024", "2025", "2026")),
    }
    return {
        "n_trades": len(trades),
        "n_calendar_days": len(calendar),
        "seg_stats": base,
        "equity": ec,
        "equity_2026": ec26,
        "by_year": by_year,
        "per_pair": per_pair,
        "naked": naked_grouping(trades, pairs, max_gap),
        "gates": gates,
        "gates_all_pass": all(gates.values()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--max-gap-days", type=int, default=7)
    ap.add_argument("--out", default="/tmp/aeq/b3_calendar.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    results = {}
    calendar_raw = None
    for mode in MODES:
        print(f"\n{'='*22} calendar_mode={mode} {'='*22}", flush=True)
        opens, closes, highs, lows = load_market(
            session, symbols, start - timedelta(days=400), end, calendar_mode=mode)
        calendar = [d for d in opens.index
                    if start <= (d if isinstance(d, date) else d.date()) <= end]
        if mode == "raw":
            calendar_raw = calendar
        atr = daily_atr(highs, lows, closes)
        contract_map = main_contract_surface(session, symbols, list(opens.index))
        tgt, _ = compute_targets(closes, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=args.entry_z, exit_z=args.exit_z,
                                 return_strength=True)
        t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=contract_map, coef_cache={})
        r = analyze(trades, calendar, PAIRS_ALL, args.max_gap_days)
        if mode != "raw" and calendar_raw is not None:
            # 控制变量：同一批成交放回原 raw 日历，隔离「日历变短」与「成交质量」效应
            ec_alt = equity_curve(trades, calendar_raw, denom="return")
            cal26_raw = [d for d in calendar_raw if str(d)[:4] == "2026"]
            t26 = [x for x in trades if _d(x["entry_dt"])[:4] == "2026"]
            ec26_alt = equity_curve(t26, cal26_raw, denom="return") if t26 else {"n": 0}
            r["equity_on_raw_calendar"] = ec_alt
            r["equity_2026_on_raw_calendar"] = ec26_alt
            print(f"  [对照·同一批成交放在raw日历] 总收益={ec_alt.get('total_return')} "
                  f"MDD={ec_alt.get('mdd')} 2026MDD={ec26_alt.get('mdd')}", flush=True)
        r["fu_open_missing"] = int(opens["FU888"].isna().sum()) if "FU888" in opens.columns else None
        r["sc_open_missing"] = int(opens["SC888"].isna().sum()) if "SC888" in opens.columns else None
        results[mode] = r
        print(f"  n_dates={len(opens.index)} calendar={len(calendar)} "
              f"FU888_open_missing={r['fu_open_missing']} SC888_open_missing={r['sc_open_missing']}",
              flush=True)
        print(f"  n_trades={r['n_trades']} mean_net_R={r['seg_stats'].get('mean_net_R')} "
              f"PF={r['seg_stats'].get('profit_factor')}", flush=True)
        print(f"  口径A单腿组={r['naked']['A_naked_groups']} "
              f"口径B真单腿={r['naked']['B_true_naked']} (B配对={r['naked']['B_n_paired']})",
              flush=True)
        print(f"  总收益={r['equity'].get('total_return')} MDD={r['equity'].get('mdd')} "
              f"2026MDD={r['equity_2026'].get('mdd')} 暴露={r['equity'].get('exposure_pct')}% "
              f"五门槛={r['gates_all_pass']}", flush=True)

    # 基线自检
    b = results["raw"]
    print("\n=== 基线自检（须与 §25.2 探针逐位一致）===", flush=True)
    print(f"  n_trades={b['n_trades']} (期望 195)；口径A单腿={b['naked']['A_naked_groups']} "
          f"(期望 9)；口径B真单腿={b['naked']['B_true_naked']} (期望 7)", flush=True)
    ok_base = (b["n_trades"] == 195 and b["naked"]["A_naked_groups"] == 9
               and b["naked"]["B_true_naked"] == 7)
    print(f"  基线逐位复现: {'PASS' if ok_base else 'FAIL — 须排查后再定论'}", flush=True)

    # 跨口径判定
    verdict = {"baseline_reproduced": bool(ok_base)}
    for mode in ("weekend_drop", "session_map"):
        r = results[mode]
        verdict[mode] = {
            "A_naked_groups": r["naked"]["A_naked_groups"],
            "B_true_naked": r["naked"]["B_true_naked"],
            "single_leg_disappeared_A": r["naked"]["A_naked_groups"] == 0,
            "single_leg_disappeared_B": r["naked"]["B_true_naked"] == 0,
            "five_gates_all_pass": r["gates_all_pass"],
            "mean_net_R": r["seg_stats"].get("mean_net_R"),
            "PF": r["seg_stats"].get("profit_factor"),
            "total_return": r["equity"].get("total_return"),
            "mdd": r["equity"].get("mdd"),
            "mdd_2026": r["equity_2026"].get("mdd"),
            "mdd_on_raw_calendar": r.get("equity_on_raw_calendar", {}).get("mdd"),
            "mdd_2026_on_raw_calendar": r.get("equity_2026_on_raw_calendar", {}).get("mdd"),
        }
    print("\n=== 判定 ===", flush=True)
    for k, v in verdict.items():
        print(f"  {k}: {v}", flush=True)

    report = {
        "meta": {
            "start": str(start), "end": str(end),
            "window": args.window, "entry_z": args.entry_z, "exit_z": args.exit_z,
            "pair_set": "base+ext", "n_pairs": len(PAIRS_ALL),
            "rebalance_n": 1, "sl_mult": SL_MULT, "max_gap_days": args.max_gap_days,
            "purpose": "六期 P1 日历口径修正后 B3 重跑（§26）",
            "caveats": [
                "calendar_mode 默认 raw，既有研究逐位不变；本脚本为并排对照，不改已有结论",
                "weekend_drop 丢弃周六凌晨夜盘行情，属保守简化；session_map 归位到下一交易日但仅按星期推、不处理法定节假日",
                "l3_ref.trade_calendar 仅记开市日且覆盖不全(1553行)，未用作交易日全集",
                "资金曲线沿用 §19 固定名义本金/简单加总/无复利口径，各 mode 用自己的窗口日历，Sharpe/暴露分母随之变化",
            ],
        },
        "modes": results,
        "verdict": verdict,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
