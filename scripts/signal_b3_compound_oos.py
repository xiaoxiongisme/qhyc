#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""七期 P2/P3：等风险 frac≈0.05 动态复利的样本外稳健性 + 数据边界复查。

背景：六期 P2（§26）全期情景中，唯一「无保证金超限且正收益」的档是
`size_basis=risk, frac=0.05`（NAV 1.497x / CAGR 7.18% / MDD_E0 0.320）。
但 frac=0.05 是**含 OOS 的全期扫描**选出的，本身带全样本选择偏差。本脚本检验：

  1. 逐年切片（连续账户路径，复利路径依赖不可拆）
  2. 分段重置（训练期 2021-2023 / OOS 2024-2026 各自从 E0 起算，隔离路径影响）
  3. frac ∈ {0.03,0.04,0.05,0.06,0.08} 敏感性：若仅 0.05 好 = 过拟合单点
  4. selfcheck：与六期 compound() 同参数结果必须逐位一致（保证可比）

P3 附带：复查 max_bucket 是否较六期(2026-11-02 10:00)推进；若无新增成交则
诚实声明 MDD 滚动跟踪为结构性时间缺口；lag 锚点给结论不强行抓取。

诚实边界：整数手数截断、逐日盯市用 close 近似 settle、不含资金成本/组合保证金
优惠/滑点随规模变化；复利对 E0、frac、最小 1 手敏感，属情景分析非收益承诺。

用法：python signal_b3_compound_oos.py --out /tmp/aeq/b3_compound_oos.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
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
from signal_b3_equity import _d
from signal_b3_compound import load_dim, compound, E0
from signal_b3_dd2026_s6 import dd_attribution

PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS
FRACS = (0.03, 0.04, 0.05, 0.06, 0.08)
MAX_MARGIN_PCT = 0.5
TRAIN_END = "2023-12-31"


def simulate_curve(trades, closes, calendar, dim, frac, basis,
                   max_margin_pct=MAX_MARGIN_PCT, enforce_margin=False):
    """复刻六期 compound() 逐位逻辑，额外返回完整 NAV 曲线与逐笔事件年份。

    与 compound() 唯一的差别是保留内部数组；开仓/平仓/盯市/手数公式完全相同。
    """
    by_entry, by_exit = defaultdict(list), defaultdict(list)
    for i, t in enumerate(trades):
        by_entry[_d(t["entry_dt"])].append(i)
        by_exit[_d(t["exit_dt"])].append(i)

    cal = [str(d)[:10] for d in calendar]
    close_s = closes.copy()
    close_s.index = [str(x)[:10] for x in close_s.index]

    def close_at(ds, sym):
        try:
            v = float(close_s.loc[ds, sym])
            return None if v != v else v
        except Exception:
            return None

    realized = 0.0
    open_pos = {}
    nav = []
    events = []           # {"date","kind","frac","lots"}
    peak = E0
    for ds in cal:
        for i in by_exit[ds]:
            p = open_pos.pop(i, None)
            if p is None:
                continue
            t = trades[i]
            realized += (t["pnl"] - t["cost_pts"]) * p["mult"] * p["lots"]
        floating, margin_used = 0.0, 0.0
        for i, p in open_pos.items():
            t = trades[i]
            px = close_at(ds, t["symbol"])
            if px is not None:
                floating += p["sign"] * (px - t["entry_px"]) * p["mult"] * p["lots"]
            margin_used += p["margin"]
        e_mark = E0 + realized + floating
        for i in by_entry[ds]:
            t = trades[i]
            var = t["symbol"][:-3] if t["symbol"].endswith("888") else t["symbol"]
            d = dim.get(var.upper())
            if not d or d[0] <= 0:
                events.append({"date": ds, "kind": "missing_margin_data"})
                continue
            mult, rl, rs = d
            rate = rl if t["side"] == "LONG" else rs
            if rate <= 0:
                events.append({"date": ds, "kind": "missing_margin_data"})
                continue
            margin_pl = t["entry_px"] * mult * rate
            denom = (t["entry_px"] * mult) if basis == "notional" \
                else (SL_MULT * t["risk"] * mult)
            budget = e_mark * frac
            lots = int(budget // denom) if denom > 0 else 0
            if lots < 1:
                events.append({"date": ds, "kind": "forced_one_lot"})
                lots = 1
            if margin_used + lots * margin_pl > e_mark * max_margin_pct:
                events.append({"date": ds, "kind": "margin_breach"})
                if enforce_margin:
                    events.append({"date": ds, "kind": "skipped_by_margin"})
                    continue
            sign = 1.0 if t["side"] == "LONG" else -1.0
            open_pos[i] = {"lots": lots, "mult": mult, "sign": sign,
                           "margin": lots * margin_pl}
            margin_used += lots * margin_pl
        peak = max(peak, e_mark)
        nav.append(e_mark)

    return np.array(nav, dtype=float), cal, events


def slice_stats(nav, cal, events, y0, y1):
    """对 [y0,y1] 年份区间做 NAV 起止 / 年内 MDD(÷E0) / 事件计数。"""
    idx = [i for i, d in enumerate(cal) if y0 <= d[:4] <= y1]
    if not idx:
        return None
    seg = nav[idx]
    lo, hi = seg[0], seg[-1]
    peak = lo
    dd = 0.0
    for v in seg:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / E0)
    ev = [e for e in events if y0 <= e["date"][:4] <= y1]
    kinds = defaultdict(int)
    for e in ev:
        kinds[e["kind"]] += 1
    n_events = len(ev)
    span = (date.fromisoformat(cal[idx[-1]]) - date.fromisoformat(cal[idx[0]])).days or 1
    years = span / 365.25
    mult = float(hi / lo)
    cagr = float(mult ** (1 / years) - 1) if years > 0 and mult > 0 else None
    return {
        "n_calendar_days": len(idx),
        "start_date": cal[idx[0]], "end_date": cal[idx[-1]],
        "nav_start": round(float(lo), 2), "nav_end": round(float(hi), 2),
        "nav_multiple_on_segment": round(mult, 4),
        "segment_return": round(mult - 1, 5),
        "cagr": round(cagr, 5) if cagr is not None else None,
        "mdd_over_E0": round(float(dd), 5),
        "n_events_in_segment": n_events,
        "n_forced_one_lot": kinds["forced_one_lot"],
        "n_margin_breach": kinds["margin_breach"],
        "n_skipped_by_margin": kinds["skipped_by_margin"],
        "n_missing_margin_data": kinds["missing_margin_data"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--track-end", default="2026-11-02")
    ap.add_argument("--out", default="/tmp/aeq/b3_compound_oos.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

    # ---- P3：数据边界复查（参数化查询，避免命令行引号转义）----
    row = session.execute(text(
        "SELECT max(bucket), count(*) FILTER (WHERE bucket > :cut), "
        "count(DISTINCT (bucket::date)) FILTER (WHERE bucket > :cut) "
        "FROM l1_mkt.bar_60m"
    ), {"cut": "2026-10-30 23:59:59"}).one()
    boundary = {"max_bucket": str(row[0]),
                "n_bars_after_20261030": int(row[1] or 0),
                "n_distinct_days_after_20261030": int(row[2] or 0),
                "six_phase_max_bucket": "2026-11-02 10:00",
                "advanced_vs_six_phase": str(row[0]) > "2026-11-02 10:00"}

    opens, closes, highs, lows = load_market(
        session, symbols, start - timedelta(days=400), end,
        calendar_mode=args.calendar_mode)
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
    print(f"[init] calendar_mode={args.calendar_mode} n_trades={len(trades)} "
          f"cal={len(calendar)} max_bucket={boundary['max_bucket']}", flush=True)

    # ---- selfcheck：与六期 compound() 逐位一致 ----
    r6 = compound(trades, closes, calendar, dim, 0.05, "risk",
                  enforce_margin=False, max_margin_pct=MAX_MARGIN_PCT)
    nav, cal, events = simulate_curve(trades, closes, calendar, dim, 0.05, "risk")
    mine = round(float(nav[-1] / E0), 4)
    # 与六期 compound() 完全一致的 peak 口径：peak 初值 = E0
    peak_v = E0
    mdd_v = 0.0
    for v in nav:
        peak_v = max(peak_v, v)
        mdd_v = max(mdd_v, (peak_v - v) / E0)
    mdd_v = round(float(mdd_v), 5)
    n_breach = sum(1 for e in events if e["kind"] == "margin_breach")
    selfcheck = {"six_phase_nav_multiple": r6["nav_multiple"],
                 "seven_phase_nav_multiple": mine,
                 "six_phase_mdd_over_E0": r6["mdd_over_E0"],
                 "seven_phase_mdd_over_E0": mdd_v,
                 "six_phase_n_margin_breach": r6["n_margin_breach"],
                 "seven_phase_n_margin_breach": n_breach,
                 "pass": abs(mine - r6["nav_multiple"]) < 1e-4
                         and abs(mdd_v - r6["mdd_over_E0"]) < 1e-4
                         and r6["n_margin_breach"] == n_breach}
    print(f"[selfcheck] six={r6['nav_multiple']} mine={mine} "
          f"mdd6={r6['mdd_over_E0']} mdd7={mdd_v} "
          f"breach6={r6['n_margin_breach']} breach7={n_breach} "
          f"PASS={selfcheck['pass']}", flush=True)

    # ---- P2-A：连续账户路径，逐年切片（复利路径依赖）----
    years = sorted({d[:4] for d in cal})
    yearly = {}
    for y in years:
        yearly[y] = slice_stats(nav, cal, events, y, y)

    # ---- P2-B：分段重置（训练期 / OOS 各自从 E0 起算）----
    # 用「截断 trades 到该区间」重新跑曲线，隔离训练期复利路径对 OOS 的影响
    segments = {}
    for label, (y0, y1) in {
        "train_2021_2023": ("2021", "2023"),
        "oos_2024_2026": ("2024", "2026"),
    }.items():
        sub = [x for x in trades if y0 <= _d(x["entry_dt"])[:4] <= y1]
        sub_cal = [d for d in calendar if y0 <= str(d)[:4] <= y1]
        if not sub or not sub_cal:
            segments[label] = None
            continue
        sn, sc, se = simulate_curve(sub, closes, sub_cal, dim, 0.05, "risk")
        st = slice_stats(sn, sc, se, y0, y1)
        st["n_trades_in_segment"] = len(sub)
        segments[label] = st
        print(f"[segment] {label} n={len(sub)} NAV={st['nav_multiple_on_segment']}x "
              f"MDD/E0={st['mdd_over_E0']} breach={st['n_margin_breach']} "
              f"forced1={st['n_forced_one_lot']}", flush=True)

    # ---- P2-C：frac 敏感性（全期 + OOS 分段重置）----
    fracs = {}
    for frac in FRACS:
        fn, fc, fe = simulate_curve(trades, closes, calendar, dim, frac, "risk")
        full = slice_stats(fn, fc, fe, "2021", "2026")
        sub = [x for x in trades if _d(x["entry_dt"])[:4] >= "2024"]
        sub_cal = [d for d in calendar if str(d)[:4] >= "2024"]
        sn, sc, se = simulate_curve(sub, closes, sub_cal, dim, frac, "risk")
        oos = slice_stats(sn, sc, se, "2024", "2026")
        fracs[str(frac)] = {"full_period": full, "oos_reset": oos,
                            "oos_nav_multiple": oos["nav_multiple_on_segment"],
                            "oos_mdd_over_E0": oos["mdd_over_E0"],
                            "oos_n_margin_breach": oos["n_margin_breach"]}
        print(f"[frac] {frac:.2f} full_NAV={full['nav_multiple_on_segment']}x "
              f"oos_NAV={oos['nav_multiple_on_segment']}x "
              f"oos_MDD={oos['mdd_over_E0']} oos_breach={oos['n_margin_breach']}",
              flush=True)

    # ---- P3 决定性实证：把 end 真正推进到最新可用日，看是否新增成交 / MDD 变化 ----
    # 此前「0 新增成交」是在 --end=2026-10-30 不变下得出的，属同义反复；推进 end 才是实证
    track_end = date.fromisoformat(args.track_end)
    tracking = None
    if track_end > end:
        o2, c2, h2, l2 = load_market(
            session, symbols, start - timedelta(days=400), track_end,
            calendar_mode=args.calendar_mode)
        cal2 = [d for d in o2.index
                if start <= (d if isinstance(d, date) else d.date()) <= track_end]
        atr2 = daily_atr(h2, l2, c2)
        cm2 = main_contract_surface(session, symbols, list(o2.index))
        tg2, _ = compute_targets(c2, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=args.entry_z, exit_z=args.exit_z,
                                 return_strength=True)
        t2 = tg2.reindex(index=o2.index, columns=symbols).fillna(0.0)
        tr2 = simulate_windows(t2, o2, c2, atr2, wfp, start, track_end,
                               rebalance_n=1, sl_mult=SL_MULT,
                               contract_map=cm2, coef_cache={})
        dd_base = dd_attribution(trades, calendar, PAIRS_ALL)
        dd_trk = dd_attribution(tr2, cal2, PAIRS_ALL)
        tracking = {
            "baseline_end": str(end), "tracked_end": str(track_end),
            "n_trades_baseline": len(trades), "n_trades_tracked": len(tr2),
            "n_new_trades": len(tr2) - len(trades),
            "n_calendar_days_baseline": len(calendar),
            "n_calendar_days_tracked": len(cal2),
            "mdd_2026_baseline": dd_base["mdd_2026"],
            "mdd_2026_tracked": dd_trk["mdd_2026"],
            "n_2026_trades_baseline": dd_base["n_2026_trades"],
            "n_2026_trades_tracked": dd_trk["n_2026_trades"],
            "total_return_2026_baseline": dd_base["total_return_2026"],
            "total_return_2026_tracked": dd_trk["total_return_2026"],
            "baseline_reproduce_0_10284": abs(dd_base["mdd_2026"] - 0.10284) < 1e-4,
        }
        print(f"[P3 track] end {end}->{track_end}: n_trades {len(trades)}->{len(tr2)} "
              f"(+{len(tr2)-len(trades)}) mdd2026 {dd_base['mdd_2026']}->{dd_trk['mdd_2026']} "
              f"baseline_repro={tracking['baseline_reproduce_0_10284']}", flush=True)

    report = {
        "meta": {
            "start": str(start), "end": str(end), "E0": E0,
            "calendar_mode": args.calendar_mode, "basis": "risk",
            "fracs": list(FRACS), "anchor_frac": 0.05,
            "train_end": TRAIN_END, "max_margin_pct": MAX_MARGIN_PCT,
            "purpose": "七期 P2 复利样本外稳健性 + P3 数据边界复查",
            "caveats": [
                "frac=0.05 由六期含 OOS 的全期扫描选出，本身带全样本选择偏差；本脚本即用 OOS 稳健性与 frac 敏感性检验是否为过拟合单点",
                "连续账户逐年切片为真实复利路径（路径依赖、不可拆）；分段重置为从 E0 独立起算的隔离视角，两者不可混同",
                "与六期 compound() 同公式同参数，selfcheck 逐位一致方可采信",
                "lots<1 强制 1 手、逐日盯市用 close 近似 settle、不含资金成本/组合保证金优惠/滑点随规模变化",
                "复利对 E0/frac/最小1手敏感，属情景分析而非收益承诺",
            ],
        },
        "selfcheck": selfcheck,
        "p2_continuous_by_year": yearly,
        "p2_segment_reset": segments,
        "p2_frac_sensitivity": fracs,
        "p3_boundary": boundary,
        "p3_mdd_rolling": {
            "verdict": ("structural_time_gap_confirmed" if tracking
                        and tracking["n_new_trades"] == 0
                        and tracking["mdd_2026_tracked"] == tracking["mdd_2026_baseline"]
                        else "recalc_needed"),
            "note": "此前『0 新增成交』是在 --end 不变下得出的同义反复；本段把 end 推进到最新可用日后重新实证。若 n_new_trades=0 且 MDD 不变，则确认为结构性时间缺口（新增 bar 均属 11-02 未收盘日内，无新增完整交易日）",
            "tracking": tracking,
        },
        "p3_lag_anchor": {
            "verdict": "not_started_no_change_expected",
            "note": "六期已实证统计局发布日程页/DCE 入口可达，但补规则不补逐笔 historical timestamp；§25 逐项裁定补了均不翻案（各有独立理由）。本轮不强行抓取，维持结论",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
