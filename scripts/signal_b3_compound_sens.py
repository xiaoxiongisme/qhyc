#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""八期 P1：更大 E0 / 组合保证金优惠 / 真实结算价下的复利再评估（§28.3）
   内嵌八期 P3：数据边界复查 + 真正推进 end 的 MDD 滚动跟踪实证（§28.5）。

七期 P2 在「固定 E0=1e6 / 两腿保证金全额计 / 逐日盯市用 close 近似 settle」下判定
等风险 frac 非稳健。本脚本做三组敏感性，检验哪些假设改变结论、哪些不改变：
  S1 E0 缩放 ∈ {1e6,5e6,2e7,1e8}：整数手数截断随资金规模减弱，但保证金占用比例
     与 E0 同比例放大 → 预期「强制1手」减少、「超保证金」次数不变（反直觉，须实证）
  S2 组合保证金折扣 ∈ {1.0,0.8,0.6}：模拟组合保证金优惠/净头寸，检验 P0 中
     frac=0.10 的超限是否可缓解（情景假设，非真实交易所组合保证金规则）
  S3 真实结算价：用 public.daily_bar.settle（888 主连、非空 93.4%）替换 close 近似盯市；
     **成交 pnl 仍为 open-to-open 不变（与 §19 逐位一致）**，仅影响当日权益/保证金判定
     进而影响后续开仓手数

自校验：E0=1e6 / 折扣1.0 / close 近似 / frac=0.05 / risk 全期须与六期 compound() 逐位一致
  (NAV 1.4968x、MDD_E0 0.32031、超保证金 0)。

用法：python signal_b3_compound_sens.py --out /tmp/aeq/b3_compound_sens.json
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
from app.backtest.walkforward import WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, SL_MULT)
from signal_b3_pairs import compute_targets
from signal_b3_equity import _d
from signal_b3_compound import load_dim, compound, E0
from signal_b3_compound_oos import PAIRS_ALL, MAX_MARGIN_PCT
from signal_b3_dd2026_s6 import dd_attribution

# ===== 预注册常量（运行前定死，同步写入 §28.3）=====
FRAC_ANCHOR = 0.05
BASIS = "risk"
E0_GRID = (1_000_000.0, 5_000_000.0, 20_000_000.0, 100_000_000.0)
MARGIN_DISC_GRID = (1.0, 0.8, 0.6)
OOS_Y0, OOS_Y1 = "2024", "2026"
TRACK_END = "2026-11-02"
B3_SYMS = sorted({v + "888" for p in PAIRS_ALL for v in (p[0], p[1])})


def load_settle_map(session):
    """public.daily_bar 的 888 主连真实结算价，key=(SYMBOL,'YYYY-MM-DD')。"""
    rows = session.execute(text("""
        SELECT symbol, trade_date::text, settle
        FROM public.daily_bar
        WHERE symbol LIKE '%888%' AND settle IS NOT NULL
    """)).all()
    return {(str(sym).upper(), str(td)[:10]): float(v)
            for sym, td, v in rows}


def simulate_curve_ext(trades, closes, calendar, dim, frac, basis,
                       e0=E0, margin_disc=1.0, settle_map=None,
                       max_margin_pct=MAX_MARGIN_PCT, enforce_margin=False):
    """复刻七期 simulate_curve，参数化 E0 / 保证金折扣 / 结算价源；返回价格源统计。"""
    by_entry, by_exit = defaultdict(list), defaultdict(list)
    for i, t in enumerate(trades):
        by_entry[_d(t["entry_dt"])].append(i)
        by_exit[_d(t["exit_dt"])].append(i)

    cal = [str(d)[:10] for d in calendar]
    close_s = closes.copy()
    close_s.index = [str(x)[:10] for x in close_s.index]

    def price_at(ds, sym):
        if settle_map is not None:
            v = settle_map.get((sym, ds))
            if v is not None and v == v:
                return v, "settle"
        try:
            v = float(close_s.loc[ds, sym])
            return (None if v != v else v), "close"
        except Exception:
            return None, "missing"

    realized = 0.0
    open_pos = {}
    nav, events = [], []
    ps = {"settle": 0, "close": 0, "missing": 0}
    peak = e0
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
            px, src = price_at(ds, t["symbol"])
            ps[src] += 1
            if px is not None:
                floating += p["sign"] * (px - t["entry_px"]) * p["mult"] * p["lots"]
            margin_used += p["margin"]
        e_mark = e0 + realized + floating
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
            margin_pl = t["entry_px"] * mult * rate * margin_disc
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
    return np.array(nav, dtype=float), cal, events, ps


def slice_stats_ext(nav, cal, events, y0, y1, e0):
    """对 [y0,y1] 做 NAV 起止 / 年内 MDD(÷e0) / 事件计数（与七期 slice_stats 同口径）。"""
    idx = [i for i, d in enumerate(cal) if y0 <= d[:4] <= y1]
    if not idx:
        return None
    seg = nav[idx]
    lo, hi = seg[0], seg[-1]
    peak, dd = lo, 0.0
    for v in seg:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / e0)
    ev = [e for e in events if y0 <= e["date"][:4] <= y1]
    kinds = defaultdict(int)
    for e in ev:
        kinds[e["kind"]] += 1
    span = (date.fromisoformat(cal[idx[-1]]) - date.fromisoformat(cal[idx[0]])).days or 1
    years = span / 365.25
    mult = float(hi / lo)
    cagr = float(mult ** (1 / years) - 1) if years > 0 and mult > 0 else None
    return {
        "n_calendar_days": len(idx),
        "nav_start": round(float(lo), 2), "nav_end": round(float(hi), 2),
        "nav_multiple_on_segment": round(mult, 4),
        "segment_return": round(mult - 1, 5),
        "cagr": round(cagr, 5) if cagr is not None else None,
        "mdd_over_e0": round(float(dd), 5),
        "n_forced_one_lot": kinds["forced_one_lot"],
        "n_margin_breach": kinds["margin_breach"],
        "n_skipped_by_margin": kinds["skipped_by_margin"],
        "n_missing_margin_data": kinds["missing_margin_data"],
    }


def run_pair(trades, closes, calendar, dim, frac, e0, disc, settle_map):
    """全期 + OOS 分段重置并排（各自从 e0 起算）。"""
    nf, cf, ef, psf = simulate_curve_ext(trades, closes, calendar, dim, frac,
                                         BASIS, e0=e0, margin_disc=disc,
                                         settle_map=settle_map)
    full = slice_stats_ext(nf, cf, ef, "2021", "2026", e0)
    oos_t = [x for x in trades if _d(x["entry_dt"])[:4] >= OOS_Y0]
    oos_c = [d for d in calendar if str(d)[:4] >= OOS_Y0]
    no, co, eo, pso = simulate_curve_ext(oos_t, closes, oos_c, dim, frac,
                                         BASIS, e0=e0, margin_disc=disc,
                                         settle_map=settle_map)
    oos = slice_stats_ext(no, co, eo, OOS_Y0, OOS_Y1, e0)
    return {"full_period": full, "oos_reset": oos,
            "price_source_stats_full": psf, "price_source_stats_oos": pso}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--track-end", default=TRACK_END)
    ap.add_argument("--out", default="/tmp/aeq/b3_compound_sens.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

    # ---- P3-A 边界复查（参数化）----
    row = session.execute(text(
        "SELECT max(bucket), count(*) FILTER (WHERE bucket > :cut), "
        "count(DISTINCT (bucket::date)) FILTER (WHERE bucket > :cut), "
        "count(DISTINCT symbol) FILTER (WHERE bucket > :cut) "
        "FROM l1_mkt.bar_60m"
    ), {"cut": "2026-10-30 23:59:59"}).one()
    boundary = {"max_bucket": str(row[0]),
                "n_bars_after_20261030": int(row[1] or 0),
                "n_distinct_days_after_20261030": int(row[2] or 0),
                "n_symbols_after_20261030": int(row[3] or 0),
                "seven_phase_max_bucket": "2026-11-02 11:00",
                "advanced_vs_seven_phase": str(row[0]) > "2026-11-02 11:00"}

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
    print(f"[init] n_trades={len(trades)} cal={len(calendar)} "
          f"max_bucket={boundary['max_bucket']} b3_syms={len(B3_SYMS)}", flush=True)

    settle_map = load_settle_map(session)
    hit = sum(1 for d in calendar for sym in B3_SYMS
              if (sym, str(d)[:10]) in settle_map)
    tot = len(calendar) * len(B3_SYMS)
    settle_cov = {"n_settle_map": len(settle_map),
                  "b3_calendar_cells": tot, "b3_cells_covered": hit,
                  "coverage_pct": round(100.0 * hit / tot, 2) if tot else None,
                  "note": "public.daily_bar.settle 888 主连；缺失单元格回退 close 盯市"}
    print(f"[settle] map={len(settle_map)} b3_coverage={settle_cov['coverage_pct']}%",
          flush=True)

    # 结算价量纲一致性诊断：settle/close 比率应集中 ~1，否则盯市浮动会失真
    close_s = closes.copy()
    close_s.index = [str(x)[:10] for x in close_s.index]
    ratios = []
    for d in calendar:
        ds = str(d)[:10]
        for sym in B3_SYMS:
            v = settle_map.get((sym, ds))
            if v is None or v <= 0:
                continue
            try:
                c = float(close_s.loc[ds, sym])
            except Exception:
                continue
            if c > 0 and c == c:
                ratios.append(v / c)
    if ratios:
        arr = np.array(ratios)
        bad = float(np.mean((arr < 0.5) | (arr > 2.0)))
        ratio_check = {
            "n_pairs_compared": len(ratios),
            "median": round(float(np.median(arr)), 5),
            "p01": round(float(np.percentile(arr, 1)), 5),
            "p99": round(float(np.percentile(arr, 99)), 5),
            "pct_outside_0_5_2": round(bad * 100, 4),
            "verdict": ("settle/close 量纲一致（比率集中 ~1）" if bad < 0.01
                        else "⚠ 存在量纲异常比率，S3 结果需谨慎解读"),
        }
    else:
        ratio_check = {"n_pairs_compared": 0, "verdict": "no comparable pairs"}
    print(f"[settle ratio] n={ratio_check['n_pairs_compared']} "
          f"median={ratio_check.get('median')} "
          f"outside_0.5_2={ratio_check.get('pct_outside_0_5_2')}%", flush=True)

    # ---- 自校验：与六期 compound() 逐位一致 ----
    r6 = compound(trades, closes, calendar, dim, FRAC_ANCHOR, BASIS,
                  enforce_margin=False, max_margin_pct=MAX_MARGIN_PCT)
    nav, cal, events, _ = simulate_curve_ext(trades, closes, calendar, dim,
                                             FRAC_ANCHOR, BASIS, e0=E0,
                                             margin_disc=1.0, settle_map=None)
    mine = round(float(nav[-1] / E0), 4)
    peak_v, mdd_v = E0, 0.0
    for v in nav:
        peak_v = max(peak_v, v)
        mdd_v = max(mdd_v, (peak_v - v) / E0)
    mdd_v = round(float(mdd_v), 5)
    n_breach = sum(1 for e in events if e["kind"] == "margin_breach")
    selfcheck = {"six_phase_nav": r6["nav_multiple"], "mine_nav": mine,
                 "six_phase_mdd_over_E0": r6["mdd_over_E0"], "mine_mdd_over_E0": mdd_v,
                 "six_phase_breach": r6["n_margin_breach"], "mine_breach": n_breach,
                 "pass": abs(mine - r6["nav_multiple"]) < 1e-4
                         and abs(mdd_v - r6["mdd_over_E0"]) < 1e-4
                         and r6["n_margin_breach"] == n_breach}
    print(f"[selfcheck] six={r6['nav_multiple']}x/{r6['mdd_over_E0']}/{r6['n_margin_breach']} "
          f"mine={mine}x/{mdd_v}/{n_breach} PASS={selfcheck['pass']}", flush=True)

    # ---- S1 E0 缩放 ----
    s1 = {}
    for e0 in E0_GRID:
        s1[str(int(e0))] = run_pair(trades, closes, calendar, dim, FRAC_ANCHOR,
                                    e0, 1.0, None)
        r = s1[str(int(e0))]
        print(f"[S1 E0={int(e0)}] full_NAV={r['full_period']['nav_multiple_on_segment']}x "
              f"forced1={r['full_period']['n_forced_one_lot']} "
              f"breach={r['full_period']['n_margin_breach']} "
              f"oos_NAV={r['oos_reset']['nav_multiple_on_segment']}x "
              f"oos_breach={r['oos_reset']['n_margin_breach']}", flush=True)

    # ---- S1b E0 缩放在 frac=0.10 下：实证「保证金占用比例与 E0 无关」预期 ----
    # 锚点 frac=0.05 本就 breach=0，无法验证；改用 §26 已证 breach=25 的 0.10 档
    s1b = {}
    for e0 in E0_GRID:
        r = run_pair(trades, closes, calendar, dim, 0.10, e0, 1.0, None)
        s1b[str(int(e0))] = {
            "full_NAV": r["full_period"]["nav_multiple_on_segment"],
            "full_n_margin_breach": r["full_period"]["n_margin_breach"],
            "full_n_forced_one_lot": r["full_period"]["n_forced_one_lot"],
            "oos_NAV": r["oos_reset"]["nav_multiple_on_segment"],
            "oos_n_margin_breach": r["oos_reset"]["n_margin_breach"],
        }
        print(f"[S1b frac=0.10 E0={int(e0)}] "
              f"full_NAV={s1b[str(int(e0))]['full_NAV']}x "
              f"breach={s1b[str(int(e0))]['full_n_margin_breach']} "
              f"forced1={s1b[str(int(e0))]['full_n_forced_one_lot']} "
              f"oos_breach={s1b[str(int(e0))]['oos_n_margin_breach']}", flush=True)

    # ---- S2 保证金折扣（含 P0 中 frac=0.10 超限是否缓解）----
    s2 = {}
    for disc in MARGIN_DISC_GRID:
        s2[str(disc)] = run_pair(trades, closes, calendar, dim, FRAC_ANCHOR,
                                 E0, disc, None)
        r = s2[str(disc)]
        print(f"[S2 disc={disc}] full_NAV={r['full_period']['nav_multiple_on_segment']}x "
              f"breach={r['full_period']['n_margin_breach']} "
              f"oos_breach={r['oos_reset']['n_margin_breach']}", flush=True)
    s2_frac10 = {}
    for disc in (1.0, 0.6):
        s2_frac10[str(disc)] = run_pair(trades, closes, calendar, dim, 0.10,
                                        E0, disc, None)
        r = s2_frac10[str(disc)]
        print(f"[S2 frac=0.10 disc={disc}] full_NAV={r['full_period']['nav_multiple_on_segment']}x "
              f"breach={r['full_period']['n_margin_breach']} "
              f"oos_breach={r['oos_reset']['n_margin_breach']}", flush=True)

    # ---- S3 真实结算价 vs close 近似 ----
    s3 = {
        "close_baseline": run_pair(trades, closes, calendar, dim, FRAC_ANCHOR,
                                   E0, 1.0, None),
        "settle": run_pair(trades, closes, calendar, dim, FRAC_ANCHOR,
                           E0, 1.0, settle_map),
        "settle_coverage": settle_cov,
        "settle_price_ratio_check": ratio_check,
    }
    for label in ("close_baseline", "settle"):
        r = s3[label]
        print(f"[S3 {label}] full_NAV={r['full_period']['nav_multiple_on_segment']}x "
              f"MDD/e0={r['full_period']['mdd_over_e0']} "
              f"oos_NAV={r['oos_reset']['nav_multiple_on_segment']}x "
              f"oos_MDD/e0={r['oos_reset']['mdd_over_e0']} "
              f"price={r['price_source_stats_full']}", flush=True)

    # ---- P3-B 决定性实证：真正推进 end，看新增成交 / MDD ----
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
            "baseline_reproduce_0_10284": abs(dd_base["mdd_2026"] - 0.10284) < 1e-4,
        }
        print(f"[P3 track] end {end}->{track_end}: n_trades {len(trades)}->{len(tr2)} "
              f"(+{len(tr2)-len(trades)}) mdd2026 {dd_base['mdd_2026']}->{dd_trk['mdd_2026']} "
              f"baseline_repro={tracking['baseline_reproduce_0_10284']}", flush=True)

    report = {
        "meta": {
            "start": str(start), "end": str(end), "E0_base": E0,
            "calendar_mode": args.calendar_mode, "basis": BASIS,
            "frac_anchor": FRAC_ANCHOR,
            "e0_grid_pre_registered": [int(x) for x in E0_GRID],
            "margin_disc_grid_pre_registered": list(MARGIN_DISC_GRID),
            "settle_source": "public.daily_bar.settle (888 主连, 非空 93.4%)",
            "purpose": "八期 P1 复利三敏感性 + P3 边界/MDD 推进实证",
            "caveats": [
                "S3 结算价仅替换逐日盯市的浮动盈亏估值；成交 pnl 仍为 open-to-open、与 §19 逐位一致，故 NAV 差异只经由权益/保证金判定→后续手数间接产生",
                "S2 保证金折扣为情景假设（组合优惠/净头寸），非真实交易所组合保证金规则，不事后挑最漂亮档",
                "S1 E0 缩放时 lots∝e0、margin∝e0，保证金占用比例与 E0 无关 → 超保证金次数预期不变（须实证），仅强制1手减少",
                "逐日盯市、整数手数、lots<1 强制1手、不含资金成本/滑点随规模变化；分段重置各自从 e0 起算",
                "三敏感性均为情景分析，不构成参数选择依据（选参须训练期定、OOS 只验一次，见 §28.2）",
            ],
        },
        "selfcheck": selfcheck,
        "s1_e0_scaling": s1,
        "s1b_e0_scaling_frac_0_10": s1b,
        "s2_margin_discount": s2,
        "s2_frac_0_10_margin_discount": s2_frac10,
        "s3_settle_price": s3,
        "p3_boundary": boundary,
        "p3_mdd_rolling": {
            "verdict": ("structural_time_gap_confirmed" if tracking
                        and tracking["n_new_trades"] == 0
                        and tracking["mdd_2026_tracked"] == tracking["mdd_2026_baseline"]
                        else "recalc_needed"),
            "note": "真正把 end 推进到最新可用日重新实证（非 --end 不变的同义反复）；"
                    "若 n_new_trades=0 且 MDD 不变，则确认新增 bar 均属 11-02 未收盘日内、无新增完整交易日",
            "tracking": tracking,
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
