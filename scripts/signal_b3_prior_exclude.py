#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""五期 P3：薄毛利对「训练期先验剔除 + OOS 验证」（§11.8 待办②）。

§11.7 事后按**全样本**成本占比 >50% 剔除 FG-SA/RB-HC/PP-L 后，净 R 0.19007→0.21805、
归一收益 1.89933→1.88732（几乎持平），方向上支持生产池剔除；但剔除理由用了全样本
→ 选择偏差，不得直接固化（纪律2/4）。

本节严格预注册、不窥探 OOS：
  阶段1（训练期，entry<=2023-12-31，与 §10/§13 时间切分一致）：
    只用这期间成交计算逐对统计量，固定以下通用规则（不看 OOS 调参）：
      R1 训练期归一收益率 <= 0                           → 剔除（训练期不赚钱）
      R2a 训练期成本覆盖率 >= 训练期 Q3 分位（纯训练期定阈值）→ 剔除
      R2b 训练期成本覆盖率 >= 0.50（阈值来自 §11.7 全样本观察，有轻微信息泄漏，仅对照）
      R3 训练期成交笔数 < 5                             → 剔除（覆盖度/样本不足）
      R4 = R1 ∪ R2a ∪ R3
    另列 §11.7 事后池 {FG-SA, RB-HC, PP-L} 作对照（明确含全样本选择偏差，不可固化）。
  阶段2（OOS，entry>=2024-01-01）：对每个池**重新 compute_targets + simulate_windows**
    （非事后过滤成交，避免 targets 矩阵变化带来的口径差异），统计净 R / PF / 归一收益率 /
    2024-26 逐年 / MDD，与全池基线对比。
    另给「事后过滤全池 195 笔」口径作 §11.7 逐位可比对照。

  判定：剔除池 OOS 须「净R/PF 不劣于全池 + 归一收益不显著损失 + 逐年不崩」才考虑固化；
  多规则并排披露，不挑表现最好的那条报告（纪律4：扫格挑方向=选择偏差）。

诚实声明：此处「训练期」= 时间上 <=2023 的样本，用于定剔除规则；与 WF 的 train_days
参数（reestimate=False、不做参数重估）不是同一概念，口径与 §13 一致。训练期逐对样本量
若普遍过小，须如实报告并降低结论强度。

用法：python signal_b3_prior_exclude.py --out ...
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
from signal_b3_equity import equity_curve

PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS
POSTHOC_POOL = {"FG-SA", "RB-HC", "PP-L"}   # §11.7 事后池，仅对照
TRAIN_END = date(2023, 12, 31)
OOS_START = date(2024, 1, 1)


def ds(t):
    return str(_dt(t))[:10]


def dkey(t):
    d = _dt(t)
    return d.date() if isinstance(d, datetime) else d


def simulate_pool(closes, opens, atr, symbols, wfp, start, end, pairs,
                  contract_map, coef_cache, window=60, entry_z=2.0, exit_z=0.5):
    tgt, _ = compute_targets(closes, pairs=pairs, window=window,
                             entry_z=entry_z, exit_z=exit_z, return_strength=True)
    t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
    return simulate_windows(t, opens, closes, atr, wfp, start, end,
                            rebalance_n=1, sl_mult=SL_MULT,
                            contract_map=contract_map, coef_cache=coef_cache)


def sum_ret(ts):
    return sum((x["pnl"] - x["cost_pts"]) / x["entry_px"] for x in ts)


def train_pair_stats(trades_train):
    """训练期逐对统计（含 n=0 的对），只用训练期成交。"""
    g = defaultdict(list)
    for x in trades_train:
        g[pair_of(x["symbol"], PAIRS_ALL)].append(x)
    out = {}
    for va, vb in PAIRS_ALL:
        p = f"{va}-{vb}"
        ts = g.get(p, [])
        gross = sum(abs(x["pnl"] / x["entry_px"]) for x in ts)
        cost = sum(x["cost_pts"] / x["entry_px"] for x in ts)
        out[p] = {"n": len(ts),
                  "sum_return_px": round(sum_ret(ts), 5) if ts else 0.0,
                  "mean_net_R": round(float(np.mean([(x["pnl"] - x["cost_pts"]) / x["risk"]
                                                      for x in ts])), 5) if ts else None,
                  "cost_ratio": round(cost / gross, 5) if gross > 0 else None}
    return out


def pool_eval(trades, calendar, label, excluded):
    seg = seg_stats(trades)
    oos_cal = [d for d in calendar if dkey(d) >= OOS_START]
    ec = equity_curve(trades, oos_cal, denom="return") if trades else {"n": 0}
    by_year = {}
    for y in ("2024", "2025", "2026"):
        ty = [x for x in trades if ds(x["entry_dt"])[:4] == y]
        cy = [d for d in calendar if ds(d)[:4] == y]
        ecy = equity_curve(ty, cy, denom="return") if ty else {"n": 0}
        sy = seg_stats(ty) if ty else {}
        by_year[y] = {"n": len(ty), "sum_return_px": round(sum_ret(ty), 5),
                      "PF": sy.get("profit_factor"), "mdd": ecy.get("mdd"),
                      "mean_net_R": sy.get("mean_net_R")}
    return {"label": label, "excluded": sorted(excluded), "n_pairs": len(PAIRS_ALL) - len(excluded),
            "n": seg.get("n"), "mean_net_R": seg.get("mean_net_R"),
            "PF": seg.get("profit_factor"), "sum_return_px": round(sum_ret(trades), 5),
            "mdd": ec.get("mdd"), "sharpe_daily": ec.get("sharpe_daily"),
            "by_year": by_year}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--out", default="/tmp/b3_prior_exclude.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print(f"[init] pairs={len(PAIRS_ALL)} train<={TRAIN_END} oos>={OOS_START}", flush=True)

    opens, closes, highs, lows = load_market(session, symbols,
                                             start - timedelta(days=400), end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache = {}
    session.close(); engine.dispose()
    calendar = [d for d in opens.index if start <= dkey(d) <= end]
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    # ---------- 阶段1：训练期统计 + 预注册规则（此段不接触任何 OOS 结果）----------
    full_trades = simulate_pool(closes, opens, atr, symbols, wfp, start, end,
                                PAIRS_ALL, contract_map, coef_cache,
                                args.window, args.entry_z, args.exit_z)
    train = [x for x in full_trades if dkey(x["entry_dt"]) <= TRAIN_END]
    tstats = train_pair_stats(train)
    print(f"\n=== 阶段1 训练期成交 n={len(train)}（entry<={TRAIN_END}）===", flush=True)
    print("  pair        n   sum_return  mean_net_R  cost_ratio", flush=True)
    for p, s in tstats.items():
        print(f"  {p:10s} {s['n']:3d}  {str(s['sum_return_px']):11s}  "
              f"{str(s['mean_net_R']):10s}  {s['cost_ratio']}", flush=True)

    cr_vals = [s["cost_ratio"] for s in tstats.values() if s["cost_ratio"] is not None]
    q3 = float(np.percentile(cr_vals, 75)) if cr_vals else None
    R1 = {p for p, s in tstats.items() if s["n"] > 0 and s["sum_return_px"] <= 0}
    R2a = ({p for p, s in tstats.items()
            if s["cost_ratio"] is not None and s["cost_ratio"] >= q3}
           if q3 is not None else set())
    R2b = {p for p, s in tstats.items()
           if s["cost_ratio"] is not None and s["cost_ratio"] >= 0.50}
    R3 = {p for p, s in tstats.items() if s["n"] < 5}
    R4 = R1 | R2a | R3
    rules = {"R1_train_return_le0": R1,
             "R2a_train_costratio_ge_Q3": R2a,
             "R2b_train_costratio_ge_0.50_posthoc_threshold": R2b,
             "R3_train_n_lt5": R3,
             "R4_union_R1_R2a_R3": R4,
             "POSTHOC_11_7_fullsample": POSTHOC_POOL}
    print(f"\n  训练期 cost_ratio Q3 = {round(q3, 5) if q3 is not None else None} "
          f"(有效对 n={len(cr_vals)}/{len(PAIRS_ALL)})", flush=True)
    print("=== 预注册规则各自剔除的对（OOS 结果尚未计算）===", flush=True)
    for k, v in rules.items():
        print(f"  {k:48s} -> {sorted(v)}  (剔除{len(v)}对, 剩{len(PAIRS_ALL)-len(v)}对)", flush=True)

    # ---------- 阶段2：OOS 验证（每个池重新 simulate）----------
    print("\n=== 阶段2 OOS 验证（entry>=2024，各池重新 simulate）===", flush=True)
    baseline_oos = [x for x in full_trades if dkey(x["entry_dt"]) >= OOS_START]
    evals = {"FULL_13": pool_eval(baseline_oos, calendar, "FULL_13", set())}
    for name, excluded in rules.items():
        pool = [(a, b) for a, b in PAIRS_ALL if f"{a}-{b}" not in excluded]
        tr = simulate_pool(closes, opens, atr, symbols, wfp, start, end,
                           pool, contract_map, coef_cache,
                           args.window, args.entry_z, args.exit_z)
        tr_oos = [x for x in tr if dkey(x["entry_dt"]) >= OOS_START]
        evals[name] = pool_eval(tr_oos, calendar, name, excluded)

    # §11.7 事后过滤对照（直接从全池 OOS 成交里剔除三对，不重跑 simulate）
    posthoc_filtered = [x for x in baseline_oos
                        if pair_of(x["symbol"], PAIRS_ALL) not in POSTHOC_POOL]
    evals["POSTHOC_11_7_filter_195"] = pool_eval(
        posthoc_filtered, calendar, "POSTHOC_11_7_filter_195", POSTHOC_POOL)

    hdr = "  pool                                       n_pairs  n   净R      PF     归一收益   MDD     2024/25/26收益"
    print(hdr, flush=True)
    for name, e in evals.items():
        yrs = "/".join(str(e["by_year"][y]["sum_return_px"]) for y in ("2024", "2025", "2026"))
        print(f"  {name:42s} {e['n_pairs']:5d} {str(e['n']):4s} "
              f"{str(e['mean_net_R']):7s} {str(e['PF']):6s} "
              f"{str(e['sum_return_px']):9s} {str(e['mdd']):8s} {yrs}", flush=True)

    b = evals["FULL_13"]
    verdicts = {}
    for name, e in evals.items():
        if name == "FULL_13":
            continue
        excluded = e["excluded"]
        improved_pf = bool(e["PF"] is not None and b["PF"] is not None and e["PF"] >= b["PF"])
        ret_kept = bool(e["sum_return_px"] is not None and b["sum_return_px"] is not None
                        and e["sum_return_px"] >= 0.95 * b["sum_return_px"])
        no_year_crash = bool(all((e["by_year"][y]["n"] == 0 or
                                  (e["by_year"][y]["sum_return_px"] is not None
                                   and e["by_year"][y]["sum_return_px"] > 0))
                                 for y in ("2024", "2025", "2026")))
        # 规则空转：未剔除任何对 → 与全池逐位相同，"通过"是假象，不算候选
        rule_inert = len(excluded) == 0
        verdicts[name] = {
            "PF_not_worse": improved_pf, "return_kept_95pct": ret_kept,
            "no_year_crash": no_year_crash,
            "rule_inert_no_exclusion": rule_inert,
            "candidate_for_production_pool": bool(improved_pf and ret_kept
                                                  and no_year_crash and not rule_inert),
        }

    print("\n=== 判定（须 PF不劣 + 归一收益保留>=95% + 逐年不崩）===", flush=True)
    for name, v in verdicts.items():
        print(f"  {name:42s} {v}", flush=True)

    report = {
        "meta": {"start": str(start), "end": str(end), "window": args.window,
                 "entry_z": args.entry_z, "exit_z": args.exit_z,
                 "train_end": str(TRAIN_END), "oos_start": str(OOS_START),
                 "n_windows": len(list(_windows(start, end, wfp))),
                 "train_definition": "时间上 entry<=2023 的样本用于定剔除规则；与 WF train_days (reestimate=False) 不是同一概念，口径同 §13",
                 "preregistration": "规则阈值在阶段1 仅用训练期统计量固定；R2b 的 0.50 阈值来自 §11.7 全样本观察，有轻微信息泄漏、仅作对照；事后池 §11.7 含全样本选择偏差、不可固化",
                 "caveat": [
                     "训练期逐对样本量可能偏小（见 train_pair_stats），规则稳定性受限，结论强度须相应下调",
                     "多规则并排披露，不挑表现最好者报告（纪律4）",
                     "各池均重新 compute_targets+simulate_windows；另给事后过滤 195 笔口径与 §11.7 逐位可比",
                     "OOS 验证为同一样本内的时间外推，非独立新数据源；固化生产池前仍须更长期跟踪",
                 ]},
        "train": {"n_trades": len(train), "cost_ratio_Q3": round(q3, 5) if q3 is not None else None,
                  "n_pairs_with_cost_ratio": len(cr_vals),
                  "pair_stats": tstats},
        "preregistered_rules": {k: sorted(v) for k, v in rules.items()},
        "full_baseline_check": {"n_all": len(full_trades),
                                "n_oos": len(baseline_oos),
                                "expected_all": 195},
        "oos_evals": evals,
        "verdicts": verdicts,
        "conclusion": {
            "11_7_posthoc_reproduced_in_oos": "§11.7 事后池 OOS PF 1.749→1.926、净R 0.18809→0.22095、"
                                              "归一收益 1.37958→1.36902（几乎持平）、逐年全正，与 §11.7 全样本观察一致；"
                                              "但剔除理由用全样本成本占比，含选择偏差，仍不可固化",
            "posthoc_threshold_not_reproducible_from_train": "训练期 cost_ratio 最大仅 "
                + (str(round(max(cr_vals), 5)) if cr_vals else "NA") +
                "，远低于 §11.7 的 0.50 阈值 → R2b 训练期剔除 0 对（规则空转）。"
                "『成本占比>50%』是全样本指标，无法由训练期先验复现",
            "r3_r4_improvement_is_R_illusion": "R3/R4 净R/PF 大幅改善但归一收益腰斩"
                "（1.37958→0.70569/0.55032）；MDD 从 0.21941 骤降至 0.0001 的机械原因是剔除了 FU-SC"
                "（P2 已证其为 2026 回撤来源），属『剔掉亏损源同时损失收益』，非规则稳健",
            "only_clean_prior_rule": "纯训练期、无信息泄漏且通过判定的仅 R1（剔训练期唯一负收益对 FG-SA）："
                                     "PF 1.749→1.787、归一收益 −3.6%，改善微弱",
            "recommendation": "不建议现在固化『按品种剔除』生产池：事后池含选择偏差、先验规则或空转或收益腰斩或改善微弱。"
                              "更优先且更有依据的是 P1/P2 指向的『修复配对腿完整性 / 剔除单腿裸露成交』"
                              "（P2 已证 2026 MDD 减半、收益反升），该动作不依赖全样本选择",
        },
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
