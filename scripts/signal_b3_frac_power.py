#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九期 P1：复利参数的统计功效、滚动起点验证与 Kelly 上界（§29.3）

八期 §28.2 的结论是「训练期预注册仍选不出稳健 frac」，但那条结论建立在**一次** OOS
（2024–2026，138 笔）之上。九期把「更长独立 OOS」（§28.7 候选②）拆成三个可回答的问题：

  A 滚动起点三折：对 Y∈{2024,2025,2026}，只用 <Y 的数据按同一预注册准则选 frac，
    再在 Y 当年独立评估 → 得到 3 个互不重叠的单年 OOS 检验，并观察选择是否稳定。
    （数据边界固定在 2026-11-02，无法真正延长 OOS，故用滚动起点换取样本量以外的独立性）

  B block bootstrap：在「连续资金、无整数手数截断」的理想复利
    E_{t+1}=E_t·(1+f·R_t) 下做分块自助，给出 NAV 倍数与最大回撤的置信区间
    → 把「edge 是否支持该杠杆」与「整数截断造成的退化」两件事分开
    （近似之处：忽略两腿同时持仓的相关性；已于 caveats 声明）

  C 功效分析：以训练期每笔 R 的均值/标准差，算达到 80% 功效（双侧 5%）所需样本量，
    换算成「还需多少年 OOS」，直接回答「够不够验」而不是继续调参

  D Kelly 参考上界：仅用训练期 R 序列数值最大化 E[log(1+f·R)] 得 f*，取 1/2、1/4 Kelly，
    作为**有理论依据、非 OOS 扫描**的配仓规则，再在 OOS 上验证一次（只验一次）

用法：python signal_b3_frac_power.py --out /tmp/aeq/b3_frac_power.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine
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
from signal_b3_prereg_frac import choose_fracs
from signal_b3_riskfix import (simulate_fx, slice_stats, RISK_SCALE_FIXED,
                               PREREG_GRID_FIXED, TRAIN_Y0)

# ===== 预注册常量（运行前定死，同步写入 §29.3）=====
FOLDS = ("2024", "2025", "2026")          # 滚动起点：每年一个独立 OOS 检验
BOOT_N = 5000
BOOT_BLOCK = 14                           # ≈ sqrt(190)
BOOT_SEED = 20261102
KELLY_GRID = tuple(np.arange(0.002, 0.302, 0.002))
CAND_FRACS = (0.010, 0.0133, 0.0167, 0.020, 0.0267, 0.0333)
ALPHA, POWER = 0.05, 0.80
BASIS = "risk"


def r_series(trades):
    """按开仓时间排序的每笔净 R 序列（t["net"] 已是 (pnl-cost)/risk）。"""
    ts = sorted(trades, key=lambda x: _d(x["entry_dt"]))
    return np.array([float(x["net"]) for x in ts], dtype=float)


def cont_curve(R, f, e0=1.0):
    """连续资金、无整数截断的理想复利曲线。"""
    eq = np.empty(len(R) + 1, dtype=float)
    eq[0] = e0
    for i, r in enumerate(R):
        eq[i + 1] = eq[i] * (1.0 + f * r)
    return eq


def cont_stats(eq):
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak)      # 相对峰值回撤（连续口径）
    return {"final_multiple": round(float(eq[-1] / eq[0]), 4),
            "mdd_rel_peak": round(float(dd), 5),
            "min_equity_multiple": round(float(eq.min() / eq[0]), 4)}


def block_bootstrap(R, f, n=BOOT_N, block=BOOT_BLOCK, seed=BOOT_SEED):
    rng = np.random.default_rng(seed)
    N = len(R)
    nb = int(np.ceil(N / block))
    out = []
    for _ in range(n):
        starts = rng.integers(0, N, size=nb)
        idx = np.concatenate([(np.arange(s, s + block) % N) for s in starts])[:N]
        eq = cont_curve(R[idx], f)
        out.append(cont_stats(eq))
    fin = np.array([o["final_multiple"] for o in out])
    dd = np.array([o["mdd_rel_peak"] for o in out])
    return {
        "final_multiple": {"p05": round(float(np.percentile(fin, 5)), 4),
                           "p25": round(float(np.percentile(fin, 25)), 4),
                           "p50": round(float(np.percentile(fin, 50)), 4),
                           "p75": round(float(np.percentile(fin, 75)), 4),
                           "p95": round(float(np.percentile(fin, 95)), 4),
                           "prob_below_1": round(float((fin < 1.0).mean()), 4)},
        "mdd_rel_peak": {"p05": round(float(np.percentile(dd, 5)), 4),
                         "p50": round(float(np.percentile(dd, 50)), 4),
                         "p95": round(float(np.percentile(dd, 95)), 4)},
        "n_draws": n, "block": block, "seed": seed,
    }


def kelly(R):
    best_f, best_g = None, -1e9
    curve = []
    for f in KELLY_GRID:
        g = float(np.mean(np.log1p(f * R)))
        curve.append((round(float(f), 3), round(g, 6)))
        if g > best_g:
            best_f, best_g = float(f), g
    return best_f, best_g, curve


def power_n(mean, sd, alpha=ALPHA, power=POWER):
    from math import ceil, sqrt
    try:
        from statistics import NormalDist
        z_a = NormalDist().inv_cdf(1 - alpha / 2)
        z_b = NormalDist().inv_cdf(power)
    except Exception:  # noqa: BLE001
        z_a, z_b = 1.959963985, 0.8416212336
    if mean <= 0 or sd <= 0:
        return None
    return int(ceil(((z_a + z_b) * sd / mean) ** 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--out", default="/tmp/aeq/b3_frac_power.json")
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
    R = r_series(trades)
    print(f"[init] n_trades={len(R)} mean_R={R.mean():.5f} sd_R={R.std(ddof=1):.4f} "
          f"cal={len(calendar)}", flush=True)
    report = {"meta": {"purpose": "九期 P1：滚动起点三折 + block bootstrap + 功效 + Kelly",
                       "n_trades": len(R), "mean_net_R": round(float(R.mean()), 5),
                       "sd_net_R": round(float(R.std(ddof=1)), 5),
                       "start": str(start), "end": str(end), "E0": E0,
                       "calendar_mode": args.calendar_mode,
                       "folds": list(FOLDS), "boot": {"n": BOOT_N, "block": BOOT_BLOCK, "seed": BOOT_SEED},
                       "kelly_grid": [round(float(KELLY_GRID[0]), 3), round(float(KELLY_GRID[-1]), 3), 0.002],
                       "caveats": [
                           "连续复利口径 E_{t+1}=E_t(1+f·R_t) 忽略整数手数截断与保证金占用，是理论上限而非可实现收益",
                           "block bootstrap 假设分块近似平稳，未建模两腿同时持仓的相关性与波动聚集",
                           "滚动起点各折样本少（2024/2025/2026 各 30–60 笔），单年结论不可外推为长期稳定性",
                       ]}}

    # ---------- A 滚动起点三折 ----------
    folds = {}
    for Y in FOLDS:
        train_grid = {}
        for frac in PREREG_GRID_FIXED:
            nf, cf, ef, df, _ = simulate_fx([x for x in trades if _d(x["entry_dt"])[:4] < Y],
                                            closes,
                                            [d for d in calendar if str(d)[:4] < Y],
                                            dim, frac, BASIS, risk_scale=RISK_SCALE_FIXED)
            st = slice_stats(nf, cf, ef, TRAIN_Y0, str(int(Y) - 1), E0)
            st["mdd_over_E0"] = st["mdd_over_e0"]
            train_grid[frac] = st
        picks = choose_fracs(train_grid)
        y_t = [x for x in trades if _d(x["entry_dt"])[:4] == Y]
        y_c = [d for d in calendar if str(d)[:4] == Y]
        per = {}
        for frac in PREREG_GRID_FIXED:
            ny, cy, ey, dy, _ = simulate_fx(y_t, closes, y_c, dim, frac, BASIS,
                                            risk_scale=RISK_SCALE_FIXED)
            per[frac] = slice_stats(ny, cy, ey, Y, Y, E0)
        folds[Y] = {
            "n_trades_test": len(y_t),
            "train_years": [TRAIN_Y0, str(int(Y) - 1)],
            "picks": {k: v.get("frac") for k, v in picks.items()},
            "test_grid": {str(k): v for k, v in per.items()},
        }
        # 各准则选中档在测试年的表现 + 相对网格中位
        med = float(np.median([per[f]["nav_multiple_on_segment"] for f in PREREG_GRID_FIXED]))
        for name, p in picks.items():
            fr = p.get("frac")
            if fr is None:
                continue
            g = per[fr]
            folds[Y][name] = {
                "frac": fr,
                "test_nav": g["nav_multiple_on_segment"],
                "test_mdd": g["mdd_over_e0"],
                "test_breach": g["n_margin_breach"],
                "grid_median_nav": round(med, 4),
                "beats_grid_median": bool(g["nav_multiple_on_segment"] > med),
            }
        print(f"[A {Y}] n={len(y_t)} picks={ {k: v.get('frac') for k, v in picks.items()} } "
              f"grid_median_nav={med:.4f}", flush=True)
        for name in ("R1_structural_mid", "R4_train_min_mdd"):
            if name in folds[Y]:
                q = folds[Y][name]
                print(f"        {name} frac={q['frac']} testNAV={q['test_nav']} "
                      f"MDD={q['test_mdd']} 胜中位={q['beats_grid_median']}", flush=True)
    # 选择稳定性：各折 R1/R4 选中的 frac 序列
    for name in ("R1_structural_mid", "R4_train_min_mdd"):
        seq = [folds[Y].get(name, {}).get("frac") for Y in FOLDS]
        report.setdefault("A_selection_stability", {})[name] = {
            "frac_by_fold": seq, "all_same": len(set(seq)) == 1,
            "mean_abs_step": round(float(np.mean(np.abs(np.diff([s for s in seq if s])))), 5)
            if sum(1 for s in seq if s) > 1 else None,
        }
    report["A_rolling_origin"] = folds

    # ---------- B block bootstrap（连续口径）----------
    boot = {}
    for f in CAND_FRACS:
        eq = cont_curve(R, f)
        point = cont_stats(eq)
        boot[str(f)] = {"point": point, "boot": block_bootstrap(R, f)}
        b = boot[str(f)]["boot"]
        print(f"[B] f={f:<6.4f} 点估 {point['final_multiple']}x MDD {point['mdd_rel_peak']} | "
              f"bootstrap 5–95% NAV [{b['final_multiple']['p05']}, {b['final_multiple']['p95']}] "
              f"P(NAV<1)={b['final_multiple']['prob_below_1']} "
              f"MDD p50={b['mdd_rel_peak']['p50']}", flush=True)
    report["B_bootstrap_continuous"] = boot

    # ---------- C 功效分析 ----------
    tr_R = np.array([float(x["net"]) for x in trades if _d(x["entry_dt"])[:4] <= "2023"])
    oos_R = np.array([float(x["net"]) for x in trades if _d(x["entry_dt"])[:4] >= "2024"])
    pace = len(R) / 5.83   # 2021-01-01→2026-10-30 约 5.83 年
    p = {}
    for label, arr in (("train_2021_2023", tr_R), ("oos_2024_2026", oos_R), ("all", R)):
        m, sd = float(arr.mean()), float(arr.std(ddof=1))
        nreq = power_n(m, sd)
        p[label] = {"n": int(len(arr)), "mean_R": round(m, 5), "sd_R": round(sd, 5),
                    "sd_over_mean": round(sd / m, 3) if m else None,
                    "n_required_80pct": nreq,
                    "n_shortfall": (nreq - len(arr)) if nreq else None,
                    "years_more_at_observed_pace": round((nreq - len(arr)) / pace, 2)
                    if nreq and nreq > len(arr) else 0.0}
        print(f"[C] {label}: n={len(arr)} mean={m:.5f} sd={sd:.4f} 需要 n={nreq} "
              f"缺 {p[label]['n_shortfall']} 笔 ≈ {p[label]['years_more_at_observed_pace']} 年",
              flush=True)
    p["observed_trades_per_year"] = round(pace, 2)
    p["note"] = ("功效口径：双侧 5%、80% power、单样本均值 t 检验近似；"
                 "它检验的是「净 R 均值 >0」，不是「frac 最优」——后者自由度更高、所需样本更多")
    report["C_power"] = p

    # ---------- D Kelly 上界（仅训练期）----------
    f_star, g_star, kcurve = kelly(tr_R)
    oos_eval = {}
    for mult, label in ((1.0, "kelly_1x"), (0.5, "kelly_half"), (0.25, "kelly_quarter")):
        f = round(f_star * mult, 4)
        ny, cy, ey, dy, _ = simulate_fx([x for x in trades if _d(x["entry_dt"])[:4] >= "2024"],
                                        closes, [d for d in calendar if str(d)[:4] >= "2024"],
                                        dim, f, BASIS, risk_scale=RISK_SCALE_FIXED)
        st = slice_stats(ny, cy, ey, "2024", "2026", E0)
        eq = cont_curve(oos_R, f)
        oos_eval[label] = {"frac": f, "oos_discrete": st,
                           "oos_continuous": cont_stats(eq)}
        print(f"[D] {label} f={f} OOS(离散) NAV={st['nav_multiple_on_segment']}x "
              f"MDD={st['mdd_over_e0']} 超限={st['n_margin_breach']} | "
              f"OOS(连续) {cont_stats(eq)['final_multiple']}x", flush=True)
    report["D_kelly"] = {"train_mean_R": round(float(tr_R.mean()), 5),
                         "train_sd_R": round(float(tr_R.std(ddof=1)), 5),
                         "f_star": f_star, "g_star": round(g_star, 6),
                         "hit_grid_upper_bound": bool(f_star >= float(KELLY_GRID[-1]) - 1e-9),
                         "growth_curve_sampled": kcurve[::10],
                         "oos_eval": oos_eval,
                         "note": ("Kelly 只用训练期 R 序列，不看 OOS。若 f* 触网格上界，"
                                  "说明该方法在本策略上退化（两腿并行使实际同时风险≈2f、"
                                  "R 右偏重尾、risk=3×ATR 是止损距离而非已实现波动），"
                                  "不得作为配仓依据；此处仅作上界诊断留档。")}

    # ---------- E 口径对齐：仅已实现 vs 逐日盯市 ----------
    align = {}
    for f in CAND_FRACS:
        nreal, _, _, _, _ = simulate_fx(trades, closes, calendar, dim, f, BASIS,
                                        risk_scale=RISK_SCALE_FIXED, mtm=False)
        real_st = slice_stats(nreal, [str(d)[:10] for d in calendar],
                              [], TRAIN_Y0, "2026", E0)
        eq = cont_curve(R, f)
        cst = cont_stats(eq)
        align[str(f)] = {
            "discrete_realized_only": real_st,
            "continuous": cst,
            "nav_gap_vs_continuous": round(float(
                real_st["nav_multiple_on_segment"] - cst["final_multiple"]), 4),
            "mdd_gap_vs_continuous": round(float(
                real_st["mdd_over_e0"] - cst["mdd_rel_peak"]), 5),
        }
        print(f"[E] f={f:<6.4f} 仅已实现(离散) NAV={real_st['nav_multiple_on_segment']}x "
              f"MDD={real_st['mdd_over_e0']} vs 连续 {cst['final_multiple']}x / "
              f"{cst['mdd_rel_peak']} → 差 {align[str(f)]['nav_gap_vs_continuous']}", flush=True)
    report["E_caliber_alignment"] = {
        "cells": align,
        "note": ("连续口径只记已实现、且无整数截断；离散 mtm=False 档同样只记已实现。"
                 "两者接近即说明『§27 的深回撤』主要来自逐日盯市浮亏，而非整数截断或杠杆本身；"
                 "剩余差异来自 1 手地板与保证金判定。"),
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    print(f"[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
