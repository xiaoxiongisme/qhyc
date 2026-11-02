#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六期 P3：修后日历口径下 2026 回撤归因 + 数据边界滚动复查（§26）。

背景：§22 在 raw 口径下判定「2026 MDD 0.21941 集中于 FU-SC(−0.272)、
剔除单腿后减半」；§25.2 已把因果推断降级为观察性相关。六期 P1 进一步
证明 331 个「FU888 缺失」其实是周六夜盘伪交易日，修后单腿完全消失、
2026 MDD 变为 0.10284(weekend_drop)/0.0(session_map)。故本脚本：

  1. 复查 bar_60m 最新边界与 2026-10-30 之后新增样本（判定"滚动跟踪"是否有新数据）
  2. 在 raw / weekend_drop / session_map 三口径下拆解 2026 逐对/逐月/逐笔
  3. 定位各口径 2026 最大回撤区间(peak→trough)及其区间内成交归因
  4. 明确回答：修后「回撤集中于 FU-SC」是否翻案、残余风险来自哪里

诚实边界：若无新增交易日样本，则"延续性"属结构性时间缺口，不宣称已解决。
用法：python signal_b3_dd2026_s6.py --out /tmp/aeq/b3_dd2026_s6.json
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
from app.backtest.walkforward import WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS
from signal_b3_capacity import pair_of
from signal_b3_equity import _d

PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS
MODES = ("raw", "weekend_drop", "session_map")
YEAR = "2026"


def dd_attribution(trades, calendar, pairs):
    """2026 资金曲线 + 最大回撤区间 + 区间内逐笔/逐对归因。"""
    cal = [d for d in calendar if str(d)[:4] == YEAR]
    ds_cal = [str(d)[:10] for d in cal]
    t26 = [x for x in trades if _d(x["entry_dt"])[:4] == YEAR]
    day = defaultdict(float)
    for x in t26:
        day[_d(x["entry_dt"])] += (x["pnl"] - x["cost_pts"]) / x["entry_px"]
    r = np.array([day.get(d, 0.0) for d in ds_cal], dtype=float)
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    i_tr = int(dd.argmax())
    mdd = float(dd[i_tr])
    # 回撤起点：trough 之前 eq 达到峰值的最后位置
    peak_val = float(peak[i_tr])
    i_pk = int(np.where(eq[:i_tr + 1] == peak_val)[0][0]) if i_tr >= 0 else 0
    win_days = ds_cal[i_pk:i_tr + 1]
    win = set(win_days)
    in_win = [x for x in t26 if _d(x["entry_dt"]) in win]
    per_pair = defaultdict(float)
    for x in in_win:
        per_pair[pair_of(x["symbol"], pairs)] += (x["pnl"] - x["cost_pts"]) / x["entry_px"]
    per_pair_all = defaultdict(float)
    for x in t26:
        per_pair_all[pair_of(x["symbol"], pairs)] += (x["pnl"] - x["cost_pts"]) / x["entry_px"]
    by_month = defaultdict(float)
    for x in t26:
        by_month[_d(x["entry_dt"])[:7]] += (x["pnl"] - x["cost_pts"]) / x["entry_px"]
    return {
        "n_2026_trades": len(t26),
        "n_2026_calendar_days": len(cal),
        "total_return_2026": round(float(eq[-1]), 5) if len(eq) else 0.0,
        "mdd_2026": round(mdd, 5),
        "dd_peak_date": ds_cal[i_pk] if cal else None,
        "dd_trough_date": ds_cal[i_tr] if cal else None,
        "dd_window_days": len(win_days),
        "sum_return_in_window": round(float(r[i_pk:i_tr + 1].sum()), 5),
        "n_trades_in_window": len(in_win),
        "per_pair_in_window": {k: round(v, 5) for k, v in
                               sorted(per_pair.items(), key=lambda kv: kv[1])},
        "per_pair_all_2026": {k: round(v, 5) for k, v in
                              sorted(per_pair_all.items(), key=lambda kv: kv[1])},
        "by_month_2026": {k: round(v, 5) for k, v in sorted(by_month.items())},
        "trades_in_window": [
            {"date": _d(x["entry_dt"]), "pair": pair_of(x["symbol"], pairs),
             "symbol": x["symbol"], "side": x["side"],
             "net_return_px": round((x["pnl"] - x["cost_pts"]) / x["entry_px"], 6),
             "holding_days": x["holding_days"]}
            for x in sorted(in_win, key=lambda y: _d(y["entry_dt"]))],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--out", default="/tmp/aeq/b3_dd2026_s6.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    # ---- 1. 数据边界复查 ----
    max_bucket = str(session.execute(text("SELECT max(bucket) FROM l1_mkt.bar_60m")).scalar())
    new_bars = session.execute(text("""
        SELECT count(*), count(DISTINCT symbol) FROM l1_mkt.bar_60m
        WHERE bucket > '2026-10-30 23:59:59+08'
    """)).one()
    print(f"[boundary] max_bucket={max_bucket} bars_after_2026-10-30={new_bars[0]} "
          f"symbols={new_bars[1]}", flush=True)
    boundary = {"max_bucket": max_bucket,
                "n_bars_after_20261030": int(new_bars[0]),
                "n_symbols_after_20261030": int(new_bars[1]),
                "structural_gap": True,
                "note": "11-01/11-02 为周末；11-02 10:00 为当日未收盘 bar，未产生新成交"}

    report = {"meta": {
        "start": str(start), "end": str(end), "year": YEAR,
        "window": args.window, "entry_z": args.entry_z, "exit_z": args.exit_z,
        "rebalance_n": 1, "pair_set": "base+ext",
        "purpose": "六期 P3 修后口径 2026 回撤归因 + 数据边界复查",
        "caveats": [
            "§22 原结论基于 raw 口径（含周六夜盘伪交易日），本脚本并排修后口径",
            "session_map 改变 open 成交时点语义（夜盘 21:00），2026 MDD=0 不作生产预期",
            "滚动跟踪受数据边界约束：无新增交易日样本则属结构性时间缺口，不宣称已解决",
        ],
    }, "boundary": boundary, "modes": {}}

    for mode in MODES:
        print(f"\n{'='*18} calendar_mode={mode} {'='*18}", flush=True)
        opens, closes, highs, lows = load_market(
            session, symbols, start - timedelta(days=400), end, calendar_mode=mode)
        calendar = [d for d in opens.index
                    if start <= (d if isinstance(d, date) else d.date()) <= end]
        atr = daily_atr(highs, lows, closes)
        contract_map = main_contract_surface(session, symbols, list(opens.index))
        tgt, _ = compute_targets(closes, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=args.entry_z, exit_z=args.exit_z,
                                 return_strength=True)
        t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=contract_map, coef_cache={})
        dd = dd_attribution(trades, calendar, PAIRS_ALL)
        report["modes"][mode] = dd
        print(f"  n_2026={dd['n_2026_trades']} 总收益={dd['total_return_2026']} "
              f"MDD={dd['mdd_2026']} 区间 {dd['dd_peak_date']}->{dd['dd_trough_date']}",
              flush=True)
        print(f"  区间逐对: {dd['per_pair_in_window']}", flush=True)
        print(f"  2026逐对: {dd['per_pair_all_2026']}", flush=True)

    # ---- 判定 ----
    raw = report["modes"]["raw"]
    wd = report["modes"]["weekend_drop"]
    fusc_raw = raw["per_pair_all_2026"].get("FU-SC")
    fusc_wd = wd["per_pair_all_2026"].get("FU-SC")
    verdict = {
        "raw_mdd_2026": raw["mdd_2026"],
        "weekend_drop_mdd_2026": wd["mdd_2026"],
        "session_map_mdd_2026": report["modes"]["session_map"]["mdd_2026"],
        "fu_sc_contrib_raw_2026": fusc_raw,
        "fu_sc_contrib_weekend_drop_2026": fusc_wd,
        "fu_sc_is_worst_pair_raw": (fusc_raw is not None and
                                    fusc_raw == min(raw["per_pair_all_2026"].values())),
        "fu_sc_is_worst_pair_weekend_drop": (fusc_wd is not None and
                                             fusc_wd == min(wd["per_pair_all_2026"].values())),
        "dd_reduction_pct": round(100 * (1 - wd["mdd_2026"] / raw["mdd_2026"]), 2)
                            if raw["mdd_2026"] > 0 else None,
        "rolling_tracking_resolved": False,
        "rolling_tracking_note": "无 2026-10-30 之后新增交易日成交，延续性属结构性时间缺口；"
                                 "且基线已随日历口径修正改变，须以修后口径重新起算跟踪序列",
    }
    report["verdict"] = verdict
    print("\n=== 判定 ===", flush=True)
    for k, v in verdict.items():
        print(f"  {k}: {v}", flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
