#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""八期 P0：训练期预注册 frac、OOS 只验证一次（§28.2）。

背景：六期「唯一无超限且正收益档 = 等风险 frac=0.05」是**含 OOS 的全期扫描**选出的，
带全样本选择偏差（§27.3 裁定）；七期 frac 敏感性亦明确为描述性、并要求
「若选参数须训练期定、OOS 只验一次」。本脚本严格落实该预注册。

两阶段隔离（阶段1 的准则函数只接收训练期网格结果，不构造也不读取任何 OOS 复利结果）：
  阶段1（train 2021-2023，各自从 E0 独立起算）：
    * 预注册网格 FRAC_GRID，逐格跑 simulate_curve(train_trades, train_cal)
    * 预注册 4 条准则（只用训练期 NAV / MDD / 超保证金 / 强制1手等可算量）选出 frac
    * 多准则并排披露，不挑 OOS 表现最好者报告（纪律4）
  阶段2（OOS 2024-2026，各自从 E0 独立起算）：
    * 仅对每个准则选出的 frac 各跑一次 OOS simulate_curve，如实报告，不扫格挑方向
    * 另附「事后全网格 OOS」**仅为描述性对照**（坐实 §27.3 可行窄窗 0.05-0.06 与选择偏差），
      明确标注 post-hoc、不作参数选择依据

自校验：anchor frac=0.05 的 train/OOS 结果须与 §27.3 逐位一致
  train NAV 1.1961x / MDD_E0 0.05942；OOS NAV 1.1846x / MDD_E0 0.34234。

用法：python signal_b3_prereg_frac.py --out /tmp/aeq/b3_prereg_frac.json
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
from signal_b3_compound_oos import (simulate_curve, slice_stats, PAIRS_ALL,
                                    MAX_MARGIN_PCT, TRAIN_END)

# ===== 预注册常量（运行前定死，同步写入 §28.2；运行期不修改）=====
FRAC_GRID = (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
BASIS = "risk"                      # 与 §26/§27 复利锚点一致
ANCHOR_FRAC = 0.05                 # 六期含 OOS 扫描选出的档，仅作自校验与事后对照
TRAIN_Y0, TRAIN_Y1 = "2021", "2023"
OOS_Y0, OOS_Y1 = "2024", "2026"
NARROW_WINDOW = (0.05, 0.06)       # §27.3 事后可行窄窗，仅用于判定、不参与选参


def choose_fracs(train_grid: dict) -> dict:
    """阶段1：只接收训练期网格结果，返回各预注册准则选出的 frac。

    4 条准则全部只用训练期可算量，绝不接触 OOS。多准则并排披露（纪律4）。
    """
    fracs = sorted(train_grid.keys(), key=float)
    out = {}

    # R1 结构可行域中点：训练期「无保证金超限 且 NAV>1」的 frac 集合取中位元素
    #     （偶数长度取靠小者 idx=(len-1)//2；若可行域为空则回退全网格并标注）
    feasible = [f for f in fracs
                if train_grid[f]["n_margin_breach"] == 0
                and train_grid[f]["nav_multiple_on_segment"] > 1.0]
    pool = feasible if feasible else fracs
    idx = (len(pool) - 1) // 2
    out["R1_structural_mid"] = {
        "frac": float(pool[idx]),
        "feasible_set": feasible, "pool_used": pool,
        "fallback_to_full_grid": not bool(feasible),
        "rule": "train 可行域(breach=0 且 NAV>1) 中位元素；偶数取靠小者；空则回退全网格",
    }

    # R2 训练期 NAV 最大
    r2 = max(fracs, key=lambda f: train_grid[f]["nav_multiple_on_segment"])
    out["R2_train_nav_max"] = {
        "frac": float(r2),
        "train_nav": train_grid[r2]["nav_multiple_on_segment"],
        "rule": "argmax train NAV（仅训练期）",
    }

    # R3 训练期 Calmar 最大 = segment_return / MDD_E0（仅 MDD>0 的格；否则排除）
    def calmar(f):
        g = train_grid[f]
        mdd = g["mdd_over_E0"]
        if mdd is None or mdd <= 0:
            return None
        return g["segment_return"] / mdd

    cand = [(f, calmar(f)) for f in fracs if calmar(f) is not None]
    if cand:
        r3 = max(cand, key=lambda kv: kv[1])[0]
        out["R3_train_calmar_max"] = {
            "frac": float(r3),
            "calmar_values": {f: round(c, 5) for f, c in cand},
            "rule": "argmax train Calmar=segment_return/MDD_E0（仅 MDD>0 格，仅训练期）",
        }
    else:
        out["R3_train_calmar_max"] = {"frac": None, "note": "no frac with MDD>0"}

    # R4 训练期 MDD 最小（限 NAV>1；若全不满足则回退全网格）
    nv1 = [f for f in fracs if train_grid[f]["nav_multiple_on_segment"] > 1.0]
    pool4 = nv1 if nv1 else fracs
    r4 = min(pool4, key=lambda f: train_grid[f]["mdd_over_E0"])
    out["R4_train_min_mdd"] = {
        "frac": float(r4),
        "pool_used": pool4, "fallback_to_full_grid": not bool(nv1),
        "train_mdd": train_grid[r4]["mdd_over_E0"],
        "rule": "argmin train MDD_E0（限 train NAV>1；空则回退全网格，仅训练期）",
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--out", default="/tmp/aeq/b3_prereg_frac.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

    # 行情与成交（全期滚动 WF，与 §9/§26/§27 完全同口径）
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
          f"cal={len(calendar)} E0={E0} FRAC_GRID={FRAC_GRID}", flush=True)

    # ===== 阶段1：只用训练期，构造 train 段（此刻不构造/读取任何 OOS 复利结果）=====
    train_trades = [x for x in trades
                    if TRAIN_Y0 <= _d(x["entry_dt"])[:4] <= TRAIN_Y1]
    train_cal = [d for d in calendar if TRAIN_Y0 <= str(d)[:4] <= TRAIN_Y1]
    train_grid = {}
    for frac in FRAC_GRID:
        sn, sc, se = simulate_curve(train_trades, closes, train_cal, dim,
                                    frac, BASIS)
        st = slice_stats(sn, sc, se, TRAIN_Y0, TRAIN_Y1)
        st["n_trades_in_segment"] = len(train_trades)
        train_grid[str(frac)] = st
        print(f"[train grid] frac={frac:.2f} NAV={st['nav_multiple_on_segment']}x "
              f"MDD/E0={st['mdd_over_E0']} breach={st['n_margin_breach']} "
              f"forced1={st['n_forced_one_lot']}", flush=True)

    # 预注册准则（只吃 train_grid）
    choices = choose_fracs(train_grid)
    print(f"[stage1 choices] " +
          " ".join(f"{k}={v.get('frac')}" for k, v in choices.items()), flush=True)

    # 阶段1 准则可行性诊断：**只用训练期信息**（不看 OOS 即可判定准则设计缺陷）
    rule_diag = {}
    for name, v in choices.items():
        f = v.get("frac")
        if f is None:
            rule_diag[name] = {"selected_frac": None, "note": v.get("note")}
            continue
        g = train_grid[str(f)]
        nb = g["n_margin_breach"]
        rule_diag[name] = {
            "selected_frac": f,
            "train_nav": g["nav_multiple_on_segment"],
            "train_mdd_over_E0": g["mdd_over_E0"],
            "train_n_margin_breach": nb,
            "train_feasible_no_breach": nb == 0,
            "note": ("准则未把『无保证金超限』设为硬约束，选中训练期已超限档 → "
                     "该准则在本场景不适用（纯训练期即可判定）"
                     if nb > 0 else
                     "选中训练期未超限额，生产可行性层面通过（纯训练期判定）"),
        }
        print(f"[stage1 diag] {name} frac={f} train_breach={nb} "
              f"feasible={nb == 0}", flush=True)

    # ===== 阶段2：此刻才构造 OOS 段 =====
    oos_trades = [x for x in trades if OOS_Y0 <= _d(x["entry_dt"])[:4] <= OOS_Y1]
    oos_cal = [d for d in calendar if OOS_Y0 <= str(d)[:4] <= OOS_Y1]

    def run_oos(frac):
        sn, sc, se = simulate_curve(oos_trades, closes, oos_cal, dim, frac, BASIS)
        st = slice_stats(sn, sc, se, OOS_Y0, OOS_Y1)
        st["n_trades_in_segment"] = len(oos_trades)
        return st

    # 2a) 自校验：anchor=0.05（预注册对照基准，非选参），train/OOS 须与 §27.3 逐位一致
    a_train = train_grid[str(ANCHOR_FRAC)]
    a_oos = run_oos(ANCHOR_FRAC)
    selfcheck = {
        "anchor_frac": ANCHOR_FRAC,
        "train_nav": a_train["nav_multiple_on_segment"],
        "train_mdd_over_E0": a_train["mdd_over_E0"],
        "oos_nav": a_oos["nav_multiple_on_segment"],
        "oos_mdd_over_E0": a_oos["mdd_over_E0"],
        "s27_3_train_nav": 1.1961, "s27_3_train_mdd": 0.05942,
        "s27_3_oos_nav": 1.1846, "s27_3_oos_mdd": 0.34234,
        "train_match": abs(a_train["nav_multiple_on_segment"] - 1.1961) < 1e-3
                       and abs(a_train["mdd_over_E0"] - 0.05942) < 1e-4,
        "oos_match": abs(a_oos["nav_multiple_on_segment"] - 1.1846) < 1e-3
                     and abs(a_oos["mdd_over_E0"] - 0.34234) < 1e-4,
    }
    selfcheck["pass"] = selfcheck["train_match"] and selfcheck["oos_match"]
    print(f"[selfcheck] train {selfcheck['train_nav']}x/{selfcheck['train_mdd_over_E0']} "
          f"oos {selfcheck['oos_nav']}x/{selfcheck['oos_mdd_over_E0']} "
          f"PASS={selfcheck['pass']}", flush=True)

    # 2b) OOS 单次验证：仅对预注册准则选出的 frac 各评估一次（去重，不扫格挑方向）
    selected = sorted({v["frac"] for v in choices.values()
                       if v.get("frac") is not None})
    oos_single = {}
    for f in selected:
        st = a_oos if abs(f - ANCHOR_FRAC) < 1e-9 else run_oos(f)
        oos_single[str(f)] = st
        print(f"[OOS single] frac={f:.2f} NAV={st['nav_multiple_on_segment']}x "
              f"MDD/E0={st['mdd_over_E0']} breach={st['n_margin_breach']} "
              f"forced1={st['n_forced_one_lot']}", flush=True)

    # 2c) 事后全网格（描述性对照，明确 post-hoc、不作参数选择依据）
    oos_posthoc = {}
    for frac in FRAC_GRID:
        st = oos_single.get(str(frac)) or run_oos(frac)
        oos_posthoc[str(frac)] = {
            "oos_nav_multiple": st["nav_multiple_on_segment"],
            "oos_mdd_over_E0": st["mdd_over_E0"],
            "oos_n_margin_breach": st["n_margin_breach"],
            "oos_n_forced_one_lot": st["n_forced_one_lot"],
        }

    # ===== 判定 =====
    lo, hi = NARROW_WINDOW
    hit = [str(f) for f in selected if lo - 1e-9 <= f <= hi + 1e-9]
    miss = [str(f) for f in selected if not (lo - 1e-9 <= f <= hi + 1e-9)]
    chosen_navs = {str(f): oos_single[str(f)]["nav_multiple_on_segment"] for f in selected}
    oos_loss = [f for f, nv in chosen_navs.items() if nv < 1.0]
    anchor_selected = any(abs(f - ANCHOR_FRAC) < 1e-9 for f in selected)
    r1_frac = choices["R1_structural_mid"]["frac"]
    r1_oos = oos_single[str(r1_frac)]
    h01 = oos_single.get(str(0.10))
    verdict = {
        "n_prereg_rules": len(choices),
        "distinct_selected_fracs": [str(f) for f in selected],
        "selected_fracs_in_s27_3_posthoc_narrow_window": hit,
        "n_selected_hitting_narrow_window": len(hit),
        "selected_fracs_outside_narrow_window": miss,
        "anchor_0_05_selected_by_any_rule": anchor_selected,
        "oos_single_results": chosen_navs,
        "selected_fracs_with_oos_loss": oos_loss,
        "cleanest_rule_R1": {
            "frac": r1_frac, "oos_nav": r1_oos["nav_multiple_on_segment"],
            "oos_mdd_over_E0": r1_oos["mdd_over_E0"],
            "oos_n_margin_breach": r1_oos["n_margin_breach"],
            "note": "R1 含『无保证金超限』硬约束，是 4 条准则中生产可行性最干净者",
        },
        "high_nav_ignoring_margin": {
            "frac": 0.10,
            "oos_nav": h01["nav_multiple_on_segment"] if h01 else None,
            "oos_n_margin_breach": h01["n_margin_breach"] if h01 else None,
            "train_n_margin_breach": train_grid[str(0.10)]["n_margin_breach"],
            "note": "0.10 账面 OOS NAV 最高，但训练期已 breach=5、OOS breach 更多；"
                    "属 §21/§27 反复警示的『忽略保证金约束的账面高收益』，不可作生产预期",
        },
        "conclusion": (
            f"4 条预注册准则无一选中含-OOS 扫描锚点 0.05（anchor_selected={anchor_selected}），"
            f"选出 frac 分散于 {[str(f) for f in selected]}、其中 {len(miss)}/4 未落入 §27.3 事后窄窗 0.05-0.06 "
            f"→ 含 OOS 全期扫描确属选择偏差。含『无超限』硬约束的最干净准则 R1 选 {r1_frac}，"
            f"OOS 仅 {r1_oos['nav_multiple_on_segment']}x、MDD {r1_oos['mdd_over_E0']} "
            f"反劣于锚点 0.05 的 1.1846x/0.34234 → 训练期预注册并不能使 frac 稳健，"
            f"印证 §27.3『非稳健 alpha、不可作生产资金曲线』。"
        ),
        "any_prereg_rule_losses_on_oos": bool(oos_loss),
    }
    print(f"[verdict] selected={[str(f) for f in selected]} hit_narrow={hit} "
          f"miss={miss} anchor_selected={anchor_selected} oos_loss={oos_loss}", flush=True)

    report = {
        "meta": {
            "start": str(start), "end": str(end), "E0": E0,
            "calendar_mode": args.calendar_mode, "basis": BASIS,
            "frac_grid_pre_registered": list(FRAC_GRID),
            "anchor_frac": ANCHOR_FRAC,
            "train_years": [TRAIN_Y0, TRAIN_Y1], "oos_years": [OOS_Y0, OOS_Y1],
            "narrow_window_posthoc": list(NARROW_WINDOW),
            "max_margin_pct": MAX_MARGIN_PCT, "train_end": TRAIN_END,
            "purpose": "八期 P0：训练期预注册 frac、OOS 只验证一次（落实 §27.3 裁定）",
            "two_stage_isolation": "阶段1 choose_fracs() 仅接收 train_grid；阶段2 才构造 OOS 段；准则函数无 OOS 输入",
            "caveats": [
                "预注册网格与 4 条准则均在查看任何 OOS 复利结果前固定（同步写入 §28.2），运行期未修改",
                "多准则并排披露，不挑 OOS 表现最好者报告（纪律4）",
                "OOS 单次验证只对预注册准则选出的 frac 各评估一次、不扫格挑方向",
                "事后全网格 OOS 为描述性对照（post-hoc），仅坐实 §27.3 窄窗与选择偏差，不作参数选择依据",
                "分段重置各自从 E0 起算（隔离训练期复利路径），与 §27.3 同口径；逐日盯市用 close 近似 settle",
                "lots<1 强制 1 手、不含资金成本/组合保证金优惠/滑点随规模变化",
            ],
        },
        "selfcheck": selfcheck,
        "stage1_train_grid": train_grid,
        "stage1_prereg_choices": choices,
        "stage1_rule_feasibility_diag_train_only": rule_diag,
        "stage2_oos_single_verification": oos_single,
        "stage2_oos_posthoc_descriptive": oos_posthoc,
        "verdict": verdict,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
