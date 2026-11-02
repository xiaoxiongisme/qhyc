#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""十一期③ k* 滚动重估 + SC 无关性；④ 日历口径双报

③ 的疑点（§31.3 自己登记的）：十期选出的 k*=5 训练期只 +1.2%、OOS +12.5%，
增益几乎全部来自 2026 年的 5 对，本质等价于"避开 SC 单手爆仓"。两条检验：

  R1 滚动重估：不用全样本选 k，而是每年只用**前两年**重新选 k，再看该年表现。
     若滚动 k 仍然赚，说明"按对最小规模"不是单点拟合；若滚动后优势消失，
     说明 k*=5 是事后挑出来的。
  R2 SC 无关性：把含 SC 的配对全部剔除后重跑 base 与 k5，看增益是否还在。
     若 k5 的优势在剔除 SC 后消失 → 增益就是"SC 一个品种的爆仓规避"。

④ 日历双报：weekend_drop 与 session_map 两套日历下，逐 variant 报 NAV/MDD/年收益，
   并判定门槛（MDD<25% 且年收益>-10%）结论是否翻转。§31.5 已证 session_map 下
   基线 NAV 1.82→2.29、MDD 0.135→0.083，量级足以改变任何"崩没崩"的表述。

预注册（跑之前写死）：
  K_GRID = [1.5, 2, 3, 5, 8, None]，与十期③ 完全一致，保证可比
  滚动选择规则：k = argmax 训练期(前两年) NAV，约束训练期 MDD_peak ≤ 0.25，并列取小
  门槛：MDD_GATE=0.25，RET_GATE=-0.10
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

from signal_oos_backtest import (WalkForwardParams, daily_atr, load_market,
                                 main_contract_surface, simulate_windows)
from signal_b3_pairs import compute_targets
from signal_b3_compound_oos import PAIRS_ALL
from signal_b3_compound import load_dim
from signal_b3_riskfix import SL_MULT, simulate_fx
from signal_b3_capacity import pair_of

E0 = 1e6
FRAC = 0.02
K_GRID = [1.5, 2.0, 3.0, 5.0, 8.0, None]
MAX_MDD_TRAIN = 0.25
MDD_GATE = 0.25
RET_GATE = -0.10
YEARS = ["2024", "2025", "2026"]


def leg_risk_pct(t, dim, e0):
    code = t["symbol"][:-3] if t["symbol"].endswith("888") else t["symbol"]
    return SL_MULT * t["risk"] * dim[code.upper()][0] / e0 * 100.0


def apply_k(trades, dim, k, e0, frac):
    if k is None:
        return list(trades), 0
    target = frac * 100.0
    g = defaultdict(list)
    for t in trades:
        g[(str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL))].append(t)
    drop = {key for key, legs in g.items()
            if any(leg_risk_pct(t, dim, e0) > k * target for t in legs)}
    kept = [t for t in trades
            if (str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL)) not in drop]
    return kept, len(drop)


def apply_k_year(trades, dim, k_by_year, e0, frac):
    target = frac * 100.0
    g = defaultdict(list)
    for t in trades:
        g[(str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL))].append(t)
    drop = set()
    for key, legs in g.items():
        k = k_by_year.get(str(legs[0]["entry_dt"])[:4])
        if k is None:
            continue
        if any(leg_risk_pct(t, dim, e0) > k * target for t in legs):
            drop.add(key)
    kept = [t for t in trades
            if (str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL)) not in drop]
    return kept, len(drop)


def stats(trades, closes, calendar, dim, frac, e0):
    nav, cal, ev, diag, _ = simulate_fx(trades, closes, calendar, dim, frac, "risk",
                                        e0=e0, mtm=True)
    ret = np.asarray(nav, dtype=float) / e0
    peak = np.maximum.accumulate(ret)
    mdd = float((1.0 - ret / peak).max()) if len(ret) else 0.0
    mdd_e0 = float(max(0.0, 1.0 - float(ret.min()))) if len(ret) else 0.0
    yr = {}
    for y in range(2021, 2027):
        idx = [i for i, d in enumerate(cal) if str(d)[:4] == str(y)]
        if len(idx) < 2:
            continue
        seg = ret[idx]
        sp = np.maximum.accumulate(seg)
        yr[str(y)] = {"ret": round(float(seg[-1] / seg[0] - 1.0) if seg[0] > 0 else 0.0, 5),
                      "ret_on_e0": round(float(seg[-1] - seg[0]), 5),
                      "mdd": round(float((1.0 - seg / sp).max()), 5)}
    net = np.array([t["net"] for t in trades], dtype=float)
    return {"n_trades": len(trades),
            "nav": round(float(ret[-1]), 6),
            "ret": round(float(ret[-1] - 1.0), 6),
            "mdd_peak": round(mdd, 6), "mdd_e0": round(mdd_e0, 6),
            "net_R": round(float(net.sum()), 5) if len(net) else 0.0,
            "margin_util_max": diag.get("margin_util_max"),
            "by_year": yr}


def build(engine, session, symbols, start, end, cm):
    o, c, h, l = load_market(session, symbols, start - timedelta(days=400), end,
                             calendar_mode=cm)
    atr = daily_atr(h, l, c)
    cmap = main_contract_surface(session, symbols, list(o.index))
    tgt, _ = compute_targets(c, pairs=PAIRS_ALL, window=60, entry_z=2.0, exit_z=0.5,
                             return_strength=True)
    t = tgt.reindex(index=o.index, columns=symbols).fillna(0.0)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    tr = simulate_windows(t, o, c, atr, wfp, start, end, rebalance_n=1,
                          sl_mult=SL_MULT, contract_map=cmap, coef_cache={})
    cal = [d for d in o.index if start <= (d if isinstance(d, date) else d.date()) <= end]
    return tr, c, cal, o


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/aeq/b11_kstar.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in get_settings().main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    dim = load_dim(session)

    cache = {}
    for cm in ("weekend_drop", "session_map"):
        print(f"[build] {cm} ...", flush=True)
        cache[cm] = build(engine, session, symbols, start, end, cm)
        print(f"   n_trades={len(cache[cm][0])}", flush=True)

    out = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "start": args.start, "end": args.end, "frac": FRAC, "e0": E0,
                    "k_grid": K_GRID, "mdd_gate": MDD_GATE, "ret_gate": RET_GATE}}

    # ---------------- ③-1 滚动重估 k ----------------
    trades_wd, closes, cal_wd, opens = cache["weekend_drop"]
    print("\n[③-1] 滚动重估 k（每年只用前两年）", flush=True)
    k_by_year = {}
    grid_rows = {}
    for y in YEARS:
        y0, y1 = str(int(y) - 2), str(int(y) - 1)
        sub = [t for t in trades_wd if y0 <= str(t["entry_dt"])[:4] <= y1]
        ccal = [d for d in cal_wd if y0 <= str(d)[:4] <= y1]
        rows = []
        for k in K_GRID:
            kept, nskip = apply_k(sub, dim, k, E0, FRAC)
            st = stats(kept, closes, ccal, dim, FRAC, E0)
            rows.append({"k": k, "nav": st["nav"], "mdd": st["mdd_peak"],
                         "n_trades": st["n_trades"], "n_skip": nskip,
                         "ok": st["mdd_peak"] <= MAX_MDD_TRAIN})
        grid_rows[f"train_{y0}_{y1}"] = rows
        feas = [r for r in rows if r["ok"]] or rows
        best = max(feas, key=lambda r: (r["nav"], -abs((r["k"] or 99) - 5)))
        k_by_year[y] = best["k"]
        print(f"   训练 {y0}-{y1}: " + " ".join(
            f"k={r['k']}:{r['nav']:.4f}/{r['mdd']:.4f}" for r in rows)
            + f"  → k*={best['k']}")

    # ---------------- ③-2 SC 无关性 ----------------
    def has_sc(t):
        p = pair_of(t["symbol"], PAIRS_ALL)
        return "SC888" in p or "SC" in p

    variants = {}
    trades_base_wd = trades_wd
    for tag, trs in [("all", trades_base_wd),
                     ("noSC", [t for t in trades_base_wd if not has_sc(t)])]:
        for vname, mode in [("base", "flat"), ("k5", 5.0), ("rollk", "roll")]:
            if mode == "flat":
                kept, nskip = list(trs), 0
            elif mode == "roll":
                kept, nskip = apply_k_year(trs, dim, k_by_year, E0, FRAC)
            else:
                kept, nskip = apply_k(trs, dim, mode, E0, FRAC)
            st = stats(kept, closes, cal_wd, dim, FRAC, E0)
            st["n_skip"] = nskip
            variants[f"{tag}:{vname}"] = st
            print(f"[③-2] {tag:>4}:{vname:<5} n={st['n_trades']:>3} skip={nskip:>3} "
                  f"nav={st['nav']:.4f} mdd={st['mdd_peak']:.4f} netR={st['net_R']:+.2f}")

    # ---------------- ④ 日历双报 ----------------
    print("\n[④] 日历口径双报", flush=True)
    trades_sm, closes_sm, cal_sm, _ = cache["session_map"]
    dual = {}
    flips = []
    for tag, trs, cl, ca in [("weekend_drop", trades_base_wd, closes, cal_wd),
                             ("session_map", trades_sm, closes_sm, cal_sm)]:
        for vname, mode in [("base", "flat"), ("k5", 5.0), ("rollk", "roll")]:
            if mode == "flat":
                kept, nskip = list(trs), 0
            elif mode == "roll":
                kept, nskip = apply_k_year(trs, dim, k_by_year, E0, FRAC)
            else:
                kept, nskip = apply_k(trs, dim, mode, E0, FRAC)
            st = stats(kept, cl, ca, dim, FRAC, E0)
            st["n_skip"] = nskip
            dual[f"{tag}:{vname}"] = st
            print(f"   {tag:<12} {vname:<5} nav={st['nav']:.4f} mdd={st['mdd_peak']:.4f} "
                  f"2026={st['by_year'].get('2026', {}).get('ret')}")
    for vname in ("base", "k5", "rollk"):
        a = dual[f"weekend_drop:{vname}"]["by_year"]
        b = dual[f"session_map:{vname}"]["by_year"]
        for y in YEARS:
            if y not in a or y not in b:
                continue
            ga = (a[y]["mdd"] < MDD_GATE) and (a[y]["ret"] > RET_GATE)
            gb = (b[y]["mdd"] < MDD_GATE) and (b[y]["ret"] > RET_GATE)
            if ga != gb:
                flips.append({"variant": vname, "year": y,
                              "weekend_drop": a[y], "session_map": b[y],
                              "gate_wd": ga, "gate_sm": gb})
    ok_all = {}
    for tag in ("weekend_drop", "session_map"):
        for vname in ("base", "k5", "rollk"):
            s = dual[f"{tag}:{vname}"]
            ok_all[f"{tag}:{vname}"] = all(
                (s["by_year"][y]["mdd"] < MDD_GATE) and (s["by_year"][y]["ret"] > RET_GATE)
                for y in YEARS if y in s["by_year"])

    out.update({
        "k_rolling": {"k_by_year": k_by_year, "grid": grid_rows,
                      "rule": "argmax 训练期 NAV s.t. train MDD<=0.25（并列取小 k）"},
        "variants_weekend_drop": variants,
        "dual_calendar": dual,
        "gate_flips_between_calendars": flips,
        "five_gates_all_pass": ok_all,
    })
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)

    print(f"\n[④] 跨日历门槛翻转 {len(flips)} 处")
    for f in flips:
        print(f"   {f['variant']} {f['year']}: wd ret={f['weekend_drop']['ret']:+.3f} "
              f"mdd={f['weekend_drop']['mdd']:.3f} vs sm ret={f['session_map']['ret']:+.3f} "
              f"mdd={f['session_map']['mdd']:.3f}")
    print(f"[gates] {ok_all}")
    print(f"[done] → {args.out}")


if __name__ == "__main__":
    main()
