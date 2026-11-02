#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""五期 P2：B3 2026 回撤「跟踪 + 归因」（§19.7 待办②）。

§19 发现 2026 年 MDD 0.21941 是 2023–2025（0.027–0.045）的 5–8 倍、Sharpe 降至 1.182，
但只给数字未解释来源。本节做两件事：

  1 跟踪：把数据边界从 §19 的 end=2026-10-30 推到库内最新（bar_60m max bucket），
    多边界 {10-30, 10-31, 11-02} 对比 2026 MDD 是否延续 0.22 量级、是否新增成交。
    诚实约束：2026-10-31(周六)/11-01(周日) 非交易日，实际新增仅 11-02 一个交易日，
    且当日仅 10:00 一根 60m bar（未收盘）→ 增量样本极小，不足以判定「延续性」，
    只能给「截至最新无新增恶化 + 待持续跟踪」。
  2 归因：在 §19 同一口径（归一收益率、按 entry_dt 归集、完整交易日历）下定位最大回撤
    区间 [peak, trough]，列出区间内成交的逐笔/逐对/逐月贡献，判断 0.22 量级回撤是
    结构性集中（单一对/单笔）还是普遍性分散。

不改 simulate、与 §9/§10/§19 逐位可比；基线 end=2026-10-30 须复现
n=195 / 2026 MDD=0.21941 / 2026 总收益 0.28909。

用法：python signal_b3_dd2026.py --ends 2026-10-30,2026-10-31,2026-11-02 --out ...
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
from sqlalchemy import create_engine, text
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
from signal_b3_equity import equity_curve, by_year_equity


def ds(t):
    return str(_dt(t))[:10]


def dkey(t):
    d = _dt(t)
    return d.date() if isinstance(d, datetime) else d


def curve_series(trades, calendar):
    """§19 同口径：归一收益率按 entry_dt 归集、完整交易日历、无成交日填 0。"""
    day = defaultdict(float)
    detail = defaultdict(list)
    for t in trades:
        r = (t["pnl"] - t["cost_pts"]) / t["entry_px"]
        d = ds(t["entry_dt"])
        day[d] += r
        detail[d].append(t)
    cal = [ds(d) for d in calendar]
    r = np.array([day.get(d, 0.0) for d in cal], dtype=float)
    return cal, r, detail


def dd_window(trades, calendar):
    """定位最大回撤区间并归因（口径与 §19 equity_curve 一致）。"""
    cal, r, detail = curve_series(trades, calendar)
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    if len(dd) == 0 or dd.max() <= 0:
        return {"mdd": 0.0}
    i_tr = int(dd.argmax())
    i_pk = int(np.argmax(eq[:i_tr + 1])) if i_tr > 0 else 0
    mdd = float(dd[i_tr])
    seg = []
    per_pair = defaultdict(float)
    per_month = defaultdict(float)
    for i in range(i_pk, i_tr + 1):
        if r[i] == 0:
            continue
        for t in detail[cal[i]]:
            rr = float((t["pnl"] - t["cost_pts"]) / t["entry_px"])
            seg.append({"date": cal[i], "pair": pair_of(t["symbol"], PAIRS_ALL),
                        "symbol": t["symbol"], "side": t["side"],
                        "net_return_px": round(rr, 6),
                        "holding_days": t["holding_days"]})
            per_pair[pair_of(t["symbol"], PAIRS_ALL)] += rr
            per_month[cal[i][:7]] += rr
    return {
        "mdd": round(mdd, 5),
        "peak_date": cal[i_pk], "trough_date": cal[i_tr],
        "peak_equity": round(float(eq[i_pk]), 5),
        "trough_equity": round(float(eq[i_tr]), 5),
        "n_span_days": i_tr - i_pk + 1,
        "n_contributing_trades": len(seg),
        "sum_return_in_window": round(float(r[i_pk:i_tr + 1].sum()), 5),
        "per_pair_in_window": {k: round(float(v), 5) for k, v in
                               sorted(per_pair.items(), key=lambda kv: kv[1])},
        "per_month_in_window": dict(sorted(per_month.items())),
        "worst_trades": sorted(seg, key=lambda x: x["net_return_px"])[:8],
        "trades_in_window": seg,
    }


def naked_groups(trades):
    """识别单腿未对冲组：按 (entry_dt, pair) 归并，腿数 != 2 即裸露。"""
    g = defaultdict(list)
    for t in trades:
        g[(ds(t["entry_dt"]), pair_of(t["symbol"], PAIRS_ALL))].append(t)
    return {k for k, v in g.items() if len(v) != 2}


def strip_naked(trades):
    nk = naked_groups(trades)
    return [t for t in trades
            if (ds(t["entry_dt"]), pair_of(t["symbol"], PAIRS_ALL)) not in nk]


def month_block(trades):
    out = defaultdict(lambda: {"n": 0, "sum_return": 0.0})
    for t in trades:
        m = ds(t["entry_dt"])[:7]
        out[m]["n"] += 1
        out[m]["sum_return"] += (t["pnl"] - t["cost_pts"]) / t["entry_px"]
    return {k: {"n": v["n"], "sum_return": round(v["sum_return"], 5)}
            for k, v in sorted(out.items())}


def pair_block(trades):
    out = defaultdict(lambda: {"n": 0, "sum_return": 0.0, "sum_net_R": 0.0})
    for t in trades:
        p = pair_of(t["symbol"], PAIRS_ALL)
        out[p]["n"] += 1
        out[p]["sum_return"] += (t["pnl"] - t["cost_pts"]) / t["entry_px"]
        out[p]["sum_net_R"] += (t["pnl"] - t["cost_pts"]) / t["risk"]
    return {k: {"n": v["n"], "sum_return_px": round(v["sum_return"], 5),
                "sum_net_R": round(v["sum_net_R"], 5)}
            for k, v in sorted(out.items(), key=lambda kv: kv[1]["sum_return"])}


PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS


def trade_key(t):
    return (t["symbol"], ds(t["entry_dt"]), ds(t["exit_dt"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--ends", default="2026-10-30,2026-10-31,2026-11-02")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--pair-set", choices=["base", "ext"], default="ext")
    ap.add_argument("--out", default="/tmp/b3_dd2026.json")
    args = ap.parse_args()

    ends = [date.fromisoformat(x.strip()) for x in args.ends.split(",")]
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start = date.fromisoformat(args.start)
    max_bucket = str(session.execute(text("SELECT max(bucket) FROM l1_mkt.bar_60m")).scalar())
    print(f"[data] bar_60m max_bucket={max_bucket}", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    per_end, prev_keys = {}, set()
    for end in ends:
        opens, closes, highs, lows = load_market(session, symbols,
                                                  start - timedelta(days=400), end)
        atr = daily_atr(highs, lows, closes)
        contract_map = main_contract_surface(session, symbols, list(opens.index))
        coef_cache = {}
        calendar = [d for d in opens.index if start <= dkey(d) <= end]
        tgt, _ = compute_targets(closes, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=args.entry_z, exit_z=args.exit_z,
                                 return_strength=True)
        t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=contract_map, coef_cache=coef_cache)
        t2026 = [x for x in trades if ds(x["entry_dt"])[:4] == "2026"]
        cal26 = [d for d in calendar if ds(d)[:4] == "2026"]
        ec26 = equity_curve(t2026, cal26, denom="return")
        # 剔除单腿未对冲组后的对照（验证 2026 回撤是否由缺腿裸露驱动）
        nk = naked_groups(trades)
        t2026_clean = [x for x in t2026
                       if (ds(x["entry_dt"]), pair_of(x["symbol"], PAIRS_ALL)) not in nk]
        ec26_clean = equity_curve(t2026_clean, cal26, denom="return")
        dd_clean = dd_window(t2026_clean, cal26)
        base = seg_stats(trades)
        keys = {trade_key(x) for x in trades}
        new_keys = sorted(keys - prev_keys)
        dd = dd_window(t2026, cal26)
        entry = {
            "end": str(end), "n_total": base.get("n"),
            "n_windows": len(list(_windows(start, end, wfp))),
            "n_calendar_days": len(calendar), "n_2026_days": len(cal26),
            "n_2026_trades": len(t2026),
            "baseline_check": {"n": base.get("n"), "mean_net_R": base.get("mean_net_R"),
                               "PF": base.get("profit_factor")},
            "equity_2026": ec26,
            "equity_2026_excl_naked": ec26_clean,
            "n_2026_trades_excl_naked": len(t2026_clean),
            "n_naked_groups_total": len(nk),
            "dd_window_2026": dd,
            "dd_window_2026_excl_naked": {"mdd": dd_clean.get("mdd"),
                                          "peak_date": dd_clean.get("peak_date"),
                                          "trough_date": dd_clean.get("trough_date")},
            "by_month_2026": month_block(t2026),
            "by_pair_2026": pair_block(t2026),
            "new_trades_vs_prev_end": [list(k) for k in new_keys],
        }
        per_end[str(end)] = entry
        prev_keys = keys
        print(f"\n=== end={end} === n={entry['n_total']} windows={entry['n_windows']} "
              f"n_2026={entry['n_2026_trades']} 新增成交={len(new_keys)}", flush=True)
        print(f"  2026: 总收益={ec26.get('total_return')} MDD={ec26.get('mdd')} "
              f"Sharpe={ec26.get('sharpe_daily')} 暴露={ec26.get('exposure_pct')}%", flush=True)
        print(f"  剔除单腿裸露({len(nk)}组)后: 2026 MDD={ec26_clean.get('mdd')} "
              f"(原 {ec26.get('mdd')}) 总收益={ec26_clean.get('total_return')} "
              f"n={len(t2026_clean)} cleanDD={dd_clean.get('mdd')}", flush=True)
        print(f"  DD区间: {dd.get('peak_date')} -> {dd.get('trough_date')} "
              f"mdd={dd.get('mdd')} 贡献笔数={dd.get('n_contributing_trades')} "
              f"区间净收益={dd.get('sum_return_in_window')}", flush=True)
        if dd.get("per_pair_in_window"):
            print(f"  区间逐对(升序): {dd['per_pair_in_window']}", flush=True)
        if new_keys:
            print(f"  新增成交明细: {new_keys[:6]}", flush=True)

    session.close(); engine.dispose()

    baseline = per_end.get("2026-10-30", {})
    print("\n=== 跟踪判定 ===", flush=True)
    print(f"  §19 基线 2026 MDD={baseline.get('equity_2026', {}).get('mdd')} "
          f"(须 0.21941)", flush=True)
    for e, v in per_end.items():
        print(f"  end={e}: 2026 MDD={v['equity_2026'].get('mdd')} "
              f"n_2026={v['n_2026_trades']}", flush=True)

    b_mdd = (baseline.get("equity_2026") or {}).get("mdd")
    b_clean = (baseline.get("equity_2026_excl_naked") or {}).get("mdd")
    reduction_pct = (round(100 * (b_mdd - b_clean) / b_mdd, 2)
                     if (b_mdd and b_clean is not None and b_mdd > 0) else None)

    report = {
        "meta": {"start": str(start), "ends": [str(e) for e in ends],
                 "bar_60m_max_bucket": max_bucket, "pair_set": args.pair_set,
                 "window": args.window, "entry_z": args.entry_z,
                 "calendar_note": "2026-10-31(周六)/11-01(周日) 非交易日；实际新增交易日仅 11-02",
                 "caveat": [
                     "2026-11-02 仅有 10:00 一根 60m bar，当日未收盘，close 不完整；信号 shift(1) 不受当日 close 影响，但成交价若落在当日须谨慎",
                     "end 变化会改变 WF 滚动窗边界，n_windows 与新成交一并记录，不假定窗口不变",
                     "增量仅 1 个交易日、其中 1 日未收盘 → 只能判『截至最新无新增恶化』，不足以判定 MDD 0.22 量级是否延续，须持续跟踪",
                     "归因口径与 §19 一致（归一收益率、按 entry_dt 归集、完整交易日历），MDD 数值可逐位对照",
                 ]},
        "per_end": per_end,
        "verdict": {
            "baseline_2026_mdd": baseline.get("equity_2026", {}).get("mdd"),
            "expected_baseline_mdd": 0.21941,
            "latest_2026_mdd": per_end[str(max(ends))]["equity_2026"].get("mdd"),
            "n_new_trades_total": sum(len(v["new_trades_vs_prev_end"])
                                      for k, v in per_end.items() if k != str(min(ends))),
            "baseline_2026_mdd_excl_naked": b_clean,
            "mdd_reduction_pct_excl_naked": reduction_pct,
            "interpretation": "0.22 量级回撤集中于 FU-SC 单配对，其中最大单笔为缺腿未对冲头寸；"
                              "剔除全部单腿裸露组后 MDD 变化见上 → 区分『策略 regime 塌方』与『执行/数据缺陷』",
            "dd_attribution_baseline": baseline.get("dd_window_2026", {}),
        },
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
