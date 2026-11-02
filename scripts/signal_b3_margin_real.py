#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九期 P2：真实结算价 + 逐日真实交易所保证金率 + 组合保证金情景的联合复利（§29.4）

八期 §28.3 只用静态 `l3_ref.dim_variety` 保证金率 + close 近似盯市，并明确
「组合保证金真实规则未入库，折扣为情景假设」。九期把可实证的部分做实：

  C0  基线：close 盯市 + 静态保证金率（= §28.3 锚点口径）
  C1  真实结算价：`public.daily_bar.settle`（888 主连）盯市，成交 pnl 不变
  C2  结算价 + 逐日真实交易所保证金率：`public.futures_rule.margin_ratio`
      （2023-01-03 起 121,708 行、163 品种、单位为百分数，需 /100；
       2021–2022 无数据 → 回退静态档，已计数）
  C3  C2 + 组合保证金折扣 0.6（**情景假设，非真实规则**，因为交易所组合保证金
      细则未入库；此处只回答「若折扣成立，超限是否消失」）

判据（预注册，运行前定死）：
  - 若 C2 的保证金超限次数显著高于 C0（>5 次）→ 「frac=0.02 无需超限即可执行」的结论需修订；
  - 若 C2 与 C1 的 NAV 差 <1% 且 MDD 差 <0.01 → 动态保证金率对资金曲线不敏感，仅影响容量判定；
  - C3 只作情景，不参与任何结论。

用法：python signal_b3_margin_real.py --out /tmp/aeq/b3_margin_real.json
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
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
from signal_b3_compound import load_dim, E0
from signal_b3_compound_oos import PAIRS_ALL
from signal_b3_riskfix import simulate_fx, slice_stats, RISK_SCALE_FIXED

# ===== 预注册常量（运行前定死，同步写入 §29.4）=====
FRACS = (0.0133, 0.0167, 0.020)
BASIS = "risk"
COMBO_DISC = 0.6
BREACH_REVISE_THRESHOLD = 5
BASELINE_FRAC = 0.020


def load_settle_map(session):
    rows = session.execute(text("""
        SELECT symbol, trade_date::text, settle
        FROM public.daily_bar
        WHERE symbol LIKE '%888%' AND settle IS NOT NULL
    """)).all()
    return {(str(s).upper(), str(d)[:10]): float(v) for s, d, v in rows}


def load_dynamic_margin(session):
    """{CODE: (dates[], ratios[])}，单位由百分数转小数；NULL 跳过。"""
    rows = session.execute(text("""
        SELECT code, trade_date::text, margin_ratio, multiplier, exchange
        FROM public.futures_rule
        WHERE margin_ratio IS NOT NULL AND margin_ratio > 0
    """)).all()
    tmp = {}
    meta = {}
    for code, ds, ratio, mult, exch in rows:
        c = str(code).upper()
        tmp.setdefault(c, []).append((str(ds)[:10], float(ratio) / 100.0))
        meta.setdefault(c, {"exchange": exch, "multiplier": float(mult) if mult else None})
    out = {}
    for c, lst in tmp.items():
        lst.sort()
        out[c] = ([x[0] for x in lst], [x[1] for x in lst])
    return out, meta


def asof(series, ds):
    dates, vals = series
    i = bisect_right(dates, ds) - 1
    return vals[i] if i >= 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--out", default="/tmp/aeq/b3_margin_real.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)
    dyn, dyn_meta = load_dynamic_margin(session)
    settle_map = load_settle_map(session)
    print(f"[init] dynamic_varieties={len(dyn)} settle_keys={len(settle_map)}", flush=True)

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
    print(f"[init] n_trades={len(trades)} cal={len(calendar)}", flush=True)

    # ---- 覆盖率与静态/动态率对比（只读诊断）----
    cover = {"n_entry": len(trades), "dynamic_hit": 0, "static_fallback": 0,
             "codes": {}}
    for tr in trades:
        var = tr["symbol"][:-3] if tr["symbol"].endswith("888") else tr["symbol"]
        ds = _d(tr["entry_dt"])
        s = dyn.get(var.upper())
        r = asof(s, ds) if s else None
        if r is None:
            cover["static_fallback"] += 1
        else:
            cover["dynamic_hit"] += 1
            m = dyn_meta.get(var.upper(), {})
            cover["codes"].setdefault(var.upper(), {"n": 0, "rate_sum": 0.0,
                                                    "mult_fr": m.get("multiplier")})
            cover["codes"][var.upper()]["n"] += 1
            cover["codes"][var.upper()]["rate_sum"] += r
    for c, v in cover["codes"].items():
        v["mean_dyn_rate"] = round(v.pop("rate_sum") / v["n"], 4)
        st = dim.get(c)
        v["static_rate_long"] = round(st[1], 4) if st else None
        v["static_rate_short"] = round(st[2], 4) if st else None
        v["multiplier_dim"] = st[0] if st else None
        v["multiplier_ok"] = (v["mult_fr"] is None or not st
                              or abs(v["mult_fr"] - st[0]) < 1e-9)
    cover["hit_rate"] = round(cover["dynamic_hit"] / max(1, len(trades)), 4)
    print(f"[cover] hit={cover['dynamic_hit']}/{len(trades)} "
          f"({cover['hit_rate']:.1%}) fallback={cover['static_fallback']}", flush=True)
    for c, v in sorted(cover["codes"].items(),
                       key=lambda kv: -(kv[1]["mean_dyn_rate"] - (kv[1]["static_rate_long"] or 0)))[:6]:
        print(f"         {c}: dyn={v['mean_dyn_rate']} static={v['static_rate_long']} "
              f"n={v['n']} mult_ok={v['multiplier_ok']}", flush=True)

    def make_rate_fn(use_dyn):
        if not use_dyn:
            return None

        def fn(var, ds, side):
            s = dyn.get(var)
            return asof(s, ds) if s else None
        return fn

    combos = [
        ("C0_close_static", {"settle_map": None, "rate_fn": None, "margin_disc": 1.0}),
        ("C1_settle_static", {"settle_map": settle_map, "rate_fn": None, "margin_disc": 1.0}),
        ("C2_settle_dynamic", {"settle_map": settle_map, "rate_fn": make_rate_fn(True),
                               "margin_disc": 1.0}),
        ("C3_settle_dynamic_combo06", {"settle_map": settle_map,
                                       "rate_fn": make_rate_fn(True),
                                       "margin_disc": COMBO_DISC}),
        # C4/C5：真实保证金约束**生效**（超限则跳过该笔开仓），检验"禁固定手数+硬约束"下还剩多少 edge
        ("C4_settle_dynamic_enforce", {"settle_map": settle_map,
                                       "rate_fn": make_rate_fn(True),
                                       "margin_disc": 1.0, "enforce_margin": True}),
        ("C5_settle_dyn_combo06_enforce", {"settle_map": settle_map,
                                           "rate_fn": make_rate_fn(True),
                                           "margin_disc": COMBO_DISC, "enforce_margin": True}),
    ]

    cells = []
    for frac in FRACS:
        for name, kw in combos:
            nf, cf, ef, df, _ = simulate_fx(trades, closes, calendar, dim, frac, BASIS,
                                            risk_scale=RISK_SCALE_FIXED, **kw)
            full = slice_stats(nf, cf, ef, "2021", "2026", E0)
            oos_t = [x for x in trades if _d(x["entry_dt"])[:4] >= "2024"]
            oos_c = [d for d in calendar if str(d)[:4] >= "2024"]
            no, co, eo, do, _ = simulate_fx(oos_t, closes, oos_c, dim, frac, BASIS,
                                            risk_scale=RISK_SCALE_FIXED, **kw)
            oos = slice_stats(no, co, eo, "2024", "2026", E0)
            cells.append({"frac": frac, "combo": name, "full": full, "oos": oos,
                          "diag_full": df})
            print(f"[{name:26s} f={frac:<6.4f}] 全期 NAV={full['nav_multiple_on_segment']:>7.3f}x "
                  f"MDD={full['mdd_over_e0']:.4f} 超限={full['n_margin_breach']:>3d} | "
                  f"OOS {oos['nav_multiple_on_segment']:>6.3f}x MDD={oos['mdd_over_e0']:.4f} "
                  f"| 保证金max={df['margin_util_max']}", flush=True)

    def cell(frac, name):
        return next(c for c in cells if abs(c["frac"] - frac) < 1e-9 and c["combo"] == name)

    base = cell(BASELINE_FRAC, "C0_close_static")
    c1 = cell(BASELINE_FRAC, "C1_settle_static")
    c2 = cell(BASELINE_FRAC, "C2_settle_dynamic")
    c3 = cell(BASELINE_FRAC, "C3_settle_dynamic_combo06")
    c4 = cell(BASELINE_FRAC, "C4_settle_dynamic_enforce")
    c5 = cell(BASELINE_FRAC, "C5_settle_dyn_combo06_enforce")

    def capture(x, base_cell):
        num = x["full"]["nav_multiple_on_segment"] - 1.0
        den = base_cell["full"]["nav_multiple_on_segment"] - 1.0
        return round(num / den * 100, 2) if den else None

    verdict = {
        "c1_mdd_reduction_vs_c0": round(
            (c1["full"]["mdd_over_e0"] - base["full"]["mdd_over_e0"]) / base["full"]["mdd_over_e0"], 4),
        "c2_vs_c1_nav_gap_pct": round(
            (c2["full"]["nav_multiple_on_segment"] - c1["full"]["nav_multiple_on_segment"])
            / c1["full"]["nav_multiple_on_segment"] * 100, 3),
        "c2_vs_c1_mdd_gap": round(c2["full"]["mdd_over_e0"] - c1["full"]["mdd_over_e0"], 5),
        "c2_breach_minus_c0": c2["full"]["n_margin_breach"] - base["full"]["n_margin_breach"],
        "breach_flag_revise": bool(c2["full"]["n_margin_breach"] - base["full"]["n_margin_breach"]
                                   > BREACH_REVISE_THRESHOLD),
        "dynamic_margin_sensitive_to_curve": bool(
            abs((c2["full"]["nav_multiple_on_segment"] - c1["full"]["nav_multiple_on_segment"])
                / c1["full"]["nav_multiple_on_segment"]) >= 0.01
            or abs(c2["full"]["mdd_over_e0"] - c1["full"]["mdd_over_e0"]) >= 0.01),
        "c3_is_scenario_only": True,
        "c3_breach": c3["full"]["n_margin_breach"],
        "c4_enforce_skipped": c4["full"]["n_skipped_by_margin"],
        "c4_nav": c4["full"]["nav_multiple_on_segment"],
        "c4_mdd": c4["full"]["mdd_over_e0"],
        "c4_edge_capture_pct_vs_c2": capture(c4, c2),
        "c5_enforce_skipped": c5["full"]["n_skipped_by_margin"],
        "c5_nav": c5["full"]["nav_multiple_on_segment"],
        "c5_edge_capture_pct_vs_c2": capture(c5, c2),
        "c4_verdict": (
            "硬约束几乎无代价（edge 捕获 ≥90%），可在配仓层直接实现" if
            (capture(c4, c2) or 0) >= 90 else
            "硬约束削弱明显（edge 捕获 <90%），需先降 frac 或引入组合保证金" if
            (capture(c4, c2) or 0) >= 60 else
            "硬约束下 edge 基本被抹平（捕获 <60%），B3 需重新定容"),
    }
    print(f"[verdict] {verdict}", flush=True)

    report = {"meta": {
        "purpose": "九期 P2：真实结算价 + 逐日真实保证金率 + 组合保证金情景联合复利",
        "n_trades": len(trades), "fracs": list(FRACS), "baseline_frac": BASELINE_FRAC,
        "combo_disc_scenario": COMBO_DISC,
        "risk_scale_fixed": RISK_SCALE_FIXED, "basis": BASIS,
        "futures_rule_span": "2023-01-03 → 2026-11-02（121,708 行 / 163 品种 / 单位百分数）",
        "caveats": [
            "组合保证金真实细则未入库，C3 折扣为情景假设，不参与任何结论",
            "futures_rule 为品种级交易所最低率，非合约级、不含期货公司加收；真实占用通常更高",
            "2021–2022 无 futures_rule 数据，回退静态 dim_variety 率（已计数）",
            "成交盈亏仍为 open-to-open，与 §19/§26 逐位一致；结算价只影响当日权益与保证金判定",
        ],
    }, "coverage": cover, "cells": cells, "verdict": verdict}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
