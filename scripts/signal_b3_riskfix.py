#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九期 P0：修正复利 risk 分母历史口径偏差（重复乘 sl_mult）后重跑全链路（§29.2）

背景（§28.3 登记）：底座 `signal_oos_backtest.py:254` 的
    risk = sl_mult × 日线ATR
已是「每手止损距离（点数，含 sl_mult）」。六期起所有复利引擎却写
    denom = SL_MULT * t["risk"] * mult
即把 sl_mult=3 又乘了一遍 → 有效风险分母放大 3 倍、同等 frac 下实际风险暴露只有目标的 1/3。
七期/八期关于「frac 可行窄窗 0.05–0.06」「预注册选不出稳健 frac」的**绝对标定**因此错位，
但内部一致的自校验（同脚本内 anchor 复现）发现不了。

本脚本做四件事（严格预注册，运行前定死）：
  V0 复现：legacy（risk_scale=3.0）frac=0.05 必须逐位复现 §27.3/§28.3 锚点
     （全期 NAV 1.4968x / MDD_E0 0.32031 / 超限 0；OOS 1.1846x / 0.34234）
  V1 等价性：fixed（risk_scale=1.0）frac=0.05/3 与 legacy frac=0.05 **逐笔手数**对比
     → 若最大差异 ≤1 手且 NAV 相对误差 <1e-6，则证明该 bug 是**纯标定错误**（等价重参数化），
       不是结果错误；反之说明存在非线性交互（须逐条排查整数截断）
  V2 重标定网格：fixed 口径 frac ∈ FIXED_GRID（含真正 5%/笔 档），给出全期/OOS 的
     NAV/MDD/超限/强制1手/实际风险占权益比 → 检验「旧 frac=0.05」在生产口径下的真实含义
  V3 预注册重做：fixed 口径、网格按 1/3 重标定（与旧网格同一有效风险水平），
     复用 `signal_b3_prereg_frac.choose_fracs` 的 4 条准则，检验八期「预注册仍选不出稳健 frac」
     的定性结论是否随标定修正而改变

用法：python signal_b3_riskfix.py --out /tmp/aeq/b3_riskfix.json
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
from signal_b3_compound import load_dim, E0
from signal_b3_compound_oos import PAIRS_ALL, MAX_MARGIN_PCT
from signal_b3_prereg_frac import choose_fracs

# ===== 预注册常量（运行前定死，同步写入 §29.2）=====
RISK_SCALE_LEGACY = 3.0       # 历史口径：denom = SL_MULT × t["risk"] × mult（多乘一次 sl_mult=3）
RISK_SCALE_FIXED = 1.0        # 修正口径：denom = t["risk"] × mult
BASIS = "risk"
ANCHOR_LEGACY_FRAC = 0.05     # 七/八期锚点档（legacy 标定）
EQUIV_FRAC = ANCHOR_LEGACY_FRAC / RISK_SCALE_LEGACY   # 等价重标定档 = 0.016666…
# 重标定扫描网格：覆盖有效风险 0.4%–5%/笔，含真正 5% 档
FIXED_GRID = (0.004, 0.005, 0.0067, 0.008, 0.010, 0.0133, 0.0167,
              0.020, 0.0267, 0.0333, 0.040, 0.050)
# 预注册网格：旧网格 {0.02..0.10} ÷ 3（同一有效风险水平，仅标定不同）
PREREG_GRID_FIXED = tuple(round(f / RISK_SCALE_LEGACY, 5) for f in
                          (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10))
TRAIN_Y0, TRAIN_Y1 = "2021", "2023"
OOS_Y0, OOS_Y1 = "2024", "2026"


def simulate_fx(trades, closes, calendar, dim, frac, basis, *,
                e0=E0, risk_scale=RISK_SCALE_FIXED, max_margin_pct=MAX_MARGIN_PCT,
                enforce_margin=False, settle_map=None, rate_fn=None,
                margin_disc=1.0, mtm=True):
    """逐日复利引擎；risk_scale 控制风险分母标定（1.0=修正、3.0=历史缺陷口径）。

    返回 (nav, cal, events, diag)。与六期 compound() 同口径：peak 初值 e0、
    MDD 以 e0 归一、成交盈亏用 trade 的 open-to-open pnl 扣成本。
    """
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
                return v
        try:
            v = float(close_s.loc[ds, sym])
            return None if v != v else v
        except Exception:
            return None

    realized = 0.0
    open_pos = {}
    nav = []
    events = []
    risk_fracs = []          # 每笔开仓的「实际风险/权益」
    lots_by_trade = {}
    margin_util = []         # 每日保证金占用 / 权益
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
            px = price_at(ds, t["symbol"]) if mtm else None
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
            if rate_fn is not None:
                r_dyn = rate_fn(var.upper(), ds, t["side"])
                if r_dyn is not None and r_dyn > 0:
                    rate = float(r_dyn)
            if rate <= 0:
                events.append({"date": ds, "kind": "missing_margin_data"})
                continue
            margin_pl = t["entry_px"] * mult * rate * margin_disc
            denom = (t["entry_px"] * mult) if basis == "notional" \
                else (risk_scale * t["risk"] * mult)
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
            lots_by_trade[i] = lots
            if e_mark > 0:
                risk_fracs.append(lots * t["risk"] * mult / e_mark)
        peak = max(peak, e_mark)
        nav.append(e_mark)
        if e_mark > 0:
            margin_util.append(margin_used / e_mark)

    diag = {
        "risk_frac_mean": round(float(np.mean(risk_fracs)), 5) if risk_fracs else None,
        "risk_frac_p95": round(float(np.percentile(risk_fracs, 95)), 5) if risk_fracs else None,
        "risk_frac_max": round(float(np.max(risk_fracs)), 5) if risk_fracs else None,
        "margin_util_max": round(float(np.max(margin_util)), 4) if margin_util else None,
        "n_entry": len(risk_fracs),
    }
    return np.array(nav, dtype=float), cal, events, diag, lots_by_trade


def slice_stats(nav, cal, events, y0, y1, e0):
    """段内 NAV 起止 / MDD(÷e0) / 事件计数（与 §27.3 slice_stats 同口径）。"""
    idx = [i for i, d in enumerate(cal) if y0 <= d[:4] <= y1]
    if not idx:
        return None
    seg = nav[idx]
    lo, hi = float(seg[0]), float(seg[-1])
    peak, dd = lo, 0.0
    for v in seg:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / e0)
    kinds = defaultdict(int)
    for e in events:
        if y0 <= e["date"][:4] <= y1:
            kinds[e["kind"]] += 1
    span = (date.fromisoformat(cal[idx[-1]]) - date.fromisoformat(cal[idx[0]])).days or 1
    years = span / 365.25
    mult = hi / lo if lo > 0 else float("nan")
    cagr = float(mult ** (1 / years) - 1) if years > 0 and mult > 0 else None
    return {
        "n_calendar_days": len(idx),
        "nav_start": round(lo, 2), "nav_end": round(hi, 2),
        "nav_multiple_on_segment": round(mult, 4),
        "segment_return": round(mult - 1, 5),
        "cagr": round(cagr, 5) if cagr is not None else None,
        "mdd_over_e0": round(float(dd), 5),
        "n_forced_one_lot": kinds["forced_one_lot"],
        "n_margin_breach": kinds["margin_breach"],
        "n_skipped_by_margin": kinds["skipped_by_margin"],
        "n_missing_margin_data": kinds["missing_margin_data"],
    }


def run_cell(trades, closes, calendar, dim, frac, *, risk_scale, settle_map=None):
    """全期 + OOS 分段重置（各自从 E0 起算），返回两侧统计与诊断。"""
    nf, cf, ef, df, _ = simulate_fx(trades, closes, calendar, dim, frac, BASIS,
                                    risk_scale=risk_scale, settle_map=settle_map)
    full = slice_stats(nf, cf, ef, TRAIN_Y0, OOS_Y1, E0)
    oos_t = [x for x in trades if _d(x["entry_dt"])[:4] >= OOS_Y0]
    oos_c = [d for d in calendar if str(d)[:4] >= OOS_Y0]
    no, co, eo, do, _ = simulate_fx(oos_t, closes, oos_c, dim, frac, BASIS,
                                    risk_scale=risk_scale, settle_map=settle_map)
    oos = slice_stats(no, co, eo, OOS_Y0, OOS_Y1, E0)
    return {"frac": frac, "risk_scale": risk_scale,
            "effective_risk_per_trade": round(frac / risk_scale, 5),
            "full_period": full, "oos_reset": oos,
            "diag_full": df, "diag_oos": do}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--out", default="/tmp/aeq/b3_riskfix.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

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
    print(f"[init] mode={args.calendar_mode} n_trades={len(trades)} "
          f"cal={len(calendar)} start={start} end={end}", flush=True)

    report = {"meta": {
        "purpose": "九期 P0：修正 risk 分母重复乘 sl_mult 的历史口径偏差并重跑复利全链路",
        "start": str(start), "end": str(end), "E0": E0,
        "n_trades": len(trades), "n_calendar_days": len(calendar),
        "calendar_mode": args.calendar_mode, "basis": BASIS, "sl_mult": SL_MULT,
        "risk_scale_legacy": RISK_SCALE_LEGACY, "risk_scale_fixed": RISK_SCALE_FIXED,
        "anchor_legacy_frac": ANCHOR_LEGACY_FRAC, "equiv_frac": round(EQUIV_FRAC, 6),
        "fixed_grid": list(FIXED_GRID), "prereg_grid_fixed": list(PREREG_GRID_FIXED),
        "train_years": [TRAIN_Y0, TRAIN_Y1], "oos_years": [OOS_Y0, OOS_Y1],
        "caveats": [
            "成交集固定为同一批 trades，仅配仓手数随 frac/risk_scale 变化",
            "lots<1 强制 1 手以保成交集不变，会使小额账户实际风险高于目标 frac，已计数",
            "主口径不跳过保证金超限开仓（保全成交集）",
            "逐日盯市默认用日线 close（与 §27 一致）；真实结算价见 §29.4",
        ],
    }}

    # ---------- V0 复现历史锚点 ----------
    v0 = run_cell(trades, closes, calendar, dim, ANCHOR_LEGACY_FRAC,
                  risk_scale=RISK_SCALE_LEGACY)
    want = {"full_nav": 1.4968, "full_mdd": 0.32031, "full_breach": 0,
            "oos_nav": 1.1846, "oos_mdd": 0.34234}
    got = {"full_nav": v0["full_period"]["nav_multiple_on_segment"],
           "full_mdd": v0["full_period"]["mdd_over_e0"],
           "full_breach": v0["full_period"]["n_margin_breach"],
           "oos_nav": v0["oos_reset"]["nav_multiple_on_segment"],
           "oos_mdd": v0["oos_reset"]["mdd_over_e0"]}
    ok_v0 = all(abs(got[k] - want[k]) < 1e-4 if isinstance(want[k], float) else got[k] == want[k]
                for k in want)
    report["V0_reproduce_anchor"] = {"expected": want, "got": got, "pass": bool(ok_v0),
                                     "cell": v0}
    print(f"[V0] 复现 legacy 锚点 PASS={ok_v0} got={got}", flush=True)

    # ---------- V1 等价性：fixed(frac/3) ≡ legacy(frac) ----------
    v1 = run_cell(trades, closes, calendar, dim, EQUIV_FRAC, risk_scale=RISK_SCALE_FIXED)
    _, _, _, _, lots_legacy = simulate_fx(trades, closes, calendar, dim,
                                         ANCHOR_LEGACY_FRAC, BASIS,
                                         risk_scale=RISK_SCALE_LEGACY)
    _, _, _, _, lots_fixed = simulate_fx(trades, closes, calendar, dim,
                                        EQUIV_FRAC, BASIS,
                                        risk_scale=RISK_SCALE_FIXED)
    keys = sorted(set(lots_legacy) | set(lots_fixed))
    diffs = [abs(lots_fixed.get(k, 0) - lots_legacy.get(k, 0)) for k in keys]
    nav_rel = abs(v1["full_period"]["nav_multiple_on_segment"] - want["full_nav"]) / want["full_nav"]
    report["V1_equivalence"] = {
        "legacy": {"frac": ANCHOR_LEGACY_FRAC, "risk_scale": RISK_SCALE_LEGACY,
                   "full_nav": v0["full_period"]["nav_multiple_on_segment"],
                   "full_mdd": v0["full_period"]["mdd_over_e0"]},
        "fixed": {"frac": round(EQUIV_FRAC, 6), "risk_scale": RISK_SCALE_FIXED,
                  "full_nav": v1["full_period"]["nav_multiple_on_segment"],
                  "full_mdd": v1["full_period"]["mdd_over_e0"],
                  "oos_nav": v1["oos_reset"]["nav_multiple_on_segment"],
                  "oos_mdd": v1["oos_reset"]["mdd_over_e0"]},
        "lots_max_abs_diff": int(max(diffs)) if diffs else None,
        "lots_n_mismatch": int(sum(1 for d in diffs if d > 0)),
        "n_compared": len(keys),
        "nav_relative_error": round(float(nav_rel), 8),
        "verdict": ("纯标定错误：等价重参数化，结论实质不变" if max(diffs) <= 1 and nav_rel < 1e-6
                    else "存在非线性交互，需逐条排查整数截断/保证金判定"),
    }
    print(f"[V1] 等价性 maxdiff={max(diffs) if diffs else None} "
          f"mismatch={sum(1 for d in diffs if d > 0)}/{len(keys)} "
          f"nav_rel_err={nav_rel:.2e} → {report['V1_equivalence']['verdict']}", flush=True)

    # ---------- V2 修正口径重标定网格 ----------
    cells = []
    for frac in FIXED_GRID:
        c = run_cell(trades, closes, calendar, dim, frac, risk_scale=RISK_SCALE_FIXED)
        cells.append(c)
        f, o, d = c["full_period"], c["oos_reset"], c["diag_full"]
        print(f"[V2] frac={frac:<7.4f} 全期 NAV={f['nav_multiple_on_segment']:>7.3f}x "
              f"MDD={f['mdd_over_e0']:.4f} 超限={f['n_margin_breach']:>3d} "
              f"强制1手={f['n_forced_one_lot']:>3d} | OOS NAV={o['nav_multiple_on_segment']:>7.3f}x "
              f"MDD={o['mdd_over_e0']:.4f} 超限={o['n_margin_breach']:>3d} | "
              f"实际风险/笔={d['risk_frac_mean']:.4f} 保证金max={d['margin_util_max']}",
              flush=True)
    report["V2_fixed_grid"] = cells

    # ---------- V3 预注册重做（fixed 口径、网格 1/3 重标定）----------
    train_grid = {}
    for frac in PREREG_GRID_FIXED:
        nf, cf, ef, df, _ = simulate_fx(trades, closes, calendar, dim, frac, BASIS,
                                        risk_scale=RISK_SCALE_FIXED)
        tr = slice_stats(nf, cf, ef, TRAIN_Y0, TRAIN_Y1, E0)
        tr["mdd_over_E0"] = tr["mdd_over_e0"]   # choose_fracs 的键名兼容（同一数值）
        train_grid[frac] = tr
        print(f"[V3-stage1] frac={frac:<7.5f} train NAV={tr['nav_multiple_on_segment']:.4f}x "
              f"MDD={tr['mdd_over_e0']:.5f} 超限={tr['n_margin_breach']} "
              f"强制1手={tr['n_forced_one_lot']}", flush=True)
    picks = choose_fracs(train_grid)
    print(f"[V3-stage1] 准则选出={ {k: v.get('frac') for k, v in picks.items()} }", flush=True)
    stage2 = {}
    for name, p in picks.items():
        fr = p.get("frac")
        if fr is None:
            stage2[name] = {"frac": None}
            continue
        c = next((x for x in report["V2_fixed_grid"] if abs(x["frac"] - fr) < 1e-9), None)
        if c is None:
            c = run_cell(trades, closes, calendar, dim, fr, risk_scale=RISK_SCALE_FIXED)
        stage2[name] = {"frac": fr, "rule": p.get("rule"),
                        "train": train_grid[fr], "oos": c["oos_reset"]}
        print(f"[V3-stage2] {name} frac={fr} OOS NAV={c['oos_reset']['nav_multiple_on_segment']}x "
              f"MDD={c['oos_reset']['mdd_over_e0']}", flush=True)
    report["V3_prereg_fixed"] = {"stage1_train_grid": {str(k): v for k, v in train_grid.items()},
                                 "stage1_picks": picks, "stage2_oos": stage2}

    # ---------- V4：旧标注 vs 真实 5%/笔 ----------
    legacy_label = next(x for x in report["V2_fixed_grid"] if abs(x["frac"] - 0.05) < 1e-9)
    report["V4_label_mismatch"] = {
        "legacy_label_frac": ANCHOR_LEGACY_FRAC,
        "legacy_true_risk_per_trade": round(ANCHOR_LEGACY_FRAC / RISK_SCALE_LEGACY, 5),
        "same_label_under_fixed": {"frac": 0.05,
                                   "full": legacy_label["full_period"],
                                   "oos": legacy_label["oos_reset"],
                                   "diag": legacy_label["diag_full"]},
        "note": "若生产方按旧文档字面取 5%/笔 风险，实际是旧口径 15%，会显著过杠杆",
    }
    lf = legacy_label["full_period"]
    print(f"[V4] 字面 frac=0.05（修正口径）全期 NAV={lf['nav_multiple_on_segment']}x "
          f"MDD={lf['mdd_over_e0']} 超限={lf['n_margin_breach']}", flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
