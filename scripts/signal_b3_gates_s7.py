#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""七期 P0：修后口径完整重跑 §6 五门槛（补齐 §26.2 诚实边界）。

六期 P1 只复核了净R/PF/分年（前三门槛），安慰剂(④)与「与 V2 低相关」(⑤)
未在修后口径重跑。本脚本在三口径下并行给出全部五门槛：

  raw          —— 基线，须逐位复现 §10 ext:60:2.0（n=195 / mean_net_R 0.19007 /
                  PF 1.734 / 安慰剂 0.0%）；相关性应接近 §9.5 的 −0.038
  weekend_drop —— 主修后口径（剔周末夜盘伪交易日）
  session_map  —— 参考口径（夜盘归下一交易日，改变了 open 语义）

安慰剂：placebo_agg(n_perm, seed) —— 与 §9/§10 完全同函数同参数，
默认 n_perm=100 / seed=20261101，逐位可比。
相关性：V2 /tmp/alpha_regime_oos.json 逐笔，按 exit_dt 建「日度已实现净R流」，
与 B3 同口径日度流在多基准下对齐，Pearson/Spearman 用 numpy 自实现（零 scipy）。

不改底座、不改库。用法：
  python signal_b3_gates_s7.py --modes raw,weekend_drop --out /tmp/aeq/b3_gates_s7.json
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
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, seg_stats, placebo_agg, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS

PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS


def _pearson(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return float("nan")
    return round(float(np.corrcoef(a, b)[0, 1]), 4)


def _spearman(a, b):
    ra = pd.Series(a).rank().to_numpy(dtype=float)
    rb = pd.Series(b).rank().to_numpy(dtype=float)
    return _pearson(ra, rb)


def _daily_flow(rows, key, val):
    """按日聚合净R流。key 用 str(...)[:10]，兼容 ISO 字符串与 Timestamp/date。"""
    d = defaultdict(float)
    for r in rows:
        k = str(r[key])[:10]
        try:
            d[k] += float(r[val])
        except (TypeError, ValueError):
            continue
    return d


def corr_with_v2(trades, v2_rows, calendar):
    """门槛⑤：B3 与 V2 的日度已实现净R流相关性。多基准并排，诚实标注稀疏性。"""
    b3 = _daily_flow(trades, "exit_dt", "net")
    v2 = _daily_flow(v2_rows, "exit_dt", "R")
    cal = [str(x)[:10] for x in calendar]

    def _series(basis_days):
        return ([b3.get(d, 0.0) for d in basis_days],
                [v2.get(d, 0.0) for d in basis_days])

    out = {}
    # 基准1：并集（B3 exit 日 ∪ V2 exit 日）—— 最接近 §9.5「全样本日」
    union = sorted(set(b3) | set(v2))
    a, b = _series(union)
    out["basis_union"] = {
        "n_days": len(union), "pearson": _pearson(a, b), "spearman": _spearman(a, b),
        "n_b3_active": sum(1 for x in a if x != 0),
        "n_v2_active": sum(1 for x in b if x != 0),
        "n_overlap_active": sum(1 for x, y in zip(a, b) if x != 0 and y != 0),
    }
    # 基准2：B3 窗口日历
    a, b = _series(cal)
    out["basis_b3_calendar"] = {
        "n_days": len(cal), "pearson": _pearson(a, b), "spearman": _spearman(a, b),
        "n_b3_active": sum(1 for x in a if x != 0),
        "n_v2_active": sum(1 for x in b if x != 0),
        "n_overlap_active": sum(1 for x, y in zip(a, b) if x != 0 and y != 0),
    }
    # 基准3：仅双方都有成交的重叠日（内连接，敏感性）
    both = sorted(set(b3) & set(v2))
    a, b = _series(both)
    out["basis_overlap_only"] = {
        "n_days": len(both), "pearson": _pearson(a, b), "spearman": _spearman(a, b),
    }
    return out


def data_boundary(session):
    """P3 附带：复查 bar_60m 最新边界与 10-30 之后样本（参数化，避免引号转义）。"""
    out = {}
    out["max_bucket"] = str(session.execute(
        text("SELECT max(bucket) FROM l1_mkt.bar_60m")).scalar())
    r = session.execute(
        text("SELECT count(*), count(DISTINCT bucket::date), count(DISTINCT symbol), "
             "min(bucket), max(bucket) FROM l1_mkt.bar_60m "
             "WHERE bucket > :d"), {"d": "2026-10-30 23:59:59+08"}).fetchone()
    out["after_2026_10_30"] = {
        "n_bars": int(r[0] or 0), "n_distinct_days": int(r[1] or 0),
        "n_symbols": int(r[2] or 0),
        "min_bucket": str(r[3]) if r[3] else None,
        "max_bucket": str(r[4]) if r[4] else None,
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--modes", default="raw,weekend_drop")
    ap.add_argument("--n-perm", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20261101)
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--v2-json", default="/tmp/alpha_regime_oos.json")
    ap.add_argument("--out", default="/tmp/aeq/b3_gates_s7.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    boundary = data_boundary(session)
    print(f"[boundary] max_bucket={boundary['max_bucket']} "
          f"after_10_30={boundary['after_2026_10_30']}", flush=True)

    # V2 成交流（一次加载，跨 mode 复用）
    v2 = json.loads(Path(args.v2_json).read_text(encoding="utf-8"))
    v2_rows = v2.get("rows", v2 if isinstance(v2, list) else [])
    print(f"[v2] n_rows={len(v2_rows)} meta={v2.get('meta')}", flush=True)

    results = {}
    for mode in modes:
        print(f"\n{'='*20} calendar_mode={mode} {'='*20}", flush=True)
        opens, closes, highs, lows = load_market(
            session, symbols, start - timedelta(days=400), end, calendar_mode=mode)
        calendar = [d for d in opens.index
                    if start <= (d if isinstance(d, date) else d.date()) <= end]
        atr = daily_atr(highs, lows, closes)
        contract_map = main_contract_surface(session, symbols, list(opens.index))
        coef_cache = {}
        tgt, _ = compute_targets(closes, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=args.entry_z, exit_z=args.exit_z,
                                 return_strength=True)
        t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=contract_map, coef_cache=coef_cache)
        overall = seg_stats(trades)

        by_year_net = defaultdict(list)
        for tr in trades:
            by_year_net[str(tr["entry_dt"])[:4]].append(tr)
        by_year_stats = {y: seg_stats(v) for y, v in sorted(by_year_net.items())}

        # ④ 安慰剂（与 §9/§10 同函数同参数）
        pct = placebo_agg(t, opens, closes, atr, wfp, start, end, 1, SL_MULT,
                          overall["sum_net_R"], n_perm=args.n_perm, seed=args.seed,
                          contract_map=contract_map, coef_cache=coef_cache)

        # ⑤ 与 V2 低相关
        corr = corr_with_v2(trades, v2_rows, calendar)

        # 五门槛判定
        gates = {
            "1_net_R_positive": (overall.get("mean_net_R") or 0) > 0,
            "2_PF_ge_1_1": (overall.get("profit_factor")
                            if isinstance(overall.get("profit_factor"), (int, float)) else 0) >= 1.1,
            "3_no_year_crash_2024_2026": all(
                (by_year_stats.get(y, {}).get("mean_net_R") or 0) > 0
                for y in ("2024", "2025", "2026")),
            "4_placebo_lt_10pct": (pct == pct and pct < 10.0),
            "5_low_corr_with_V2": abs(corr["basis_union"]["pearson"]) < 0.30,
        }
        results[mode] = {
            "n_trades": len(trades),
            "n_calendar_days": len(calendar),
            "overall": overall,
            "by_year": by_year_stats,
            "placebo": {"rand_ge_actual_pct": pct, "n_perm": args.n_perm, "seed": args.seed},
            "corr_v2": corr,
            "gates": gates,
            "gates_all_pass": all(gates.values()),
        }
        print(f"  n_trades={len(trades)} mean_net_R={overall.get('mean_net_R')} "
              f"PF={overall.get('profit_factor')} sum_net_R={overall.get('sum_net_R')}", flush=True)
        print(f"  安慰剂 rand_ge_actual%={pct} (n_perm={args.n_perm}, seed={args.seed})", flush=True)
        print(f"  V2相关 Pearson(union)={corr['basis_union']['pearson']} "
              f"Spearman={corr['basis_union']['spearman']} "
              f"(重叠活跃日={corr['basis_union']['n_overlap_active']})", flush=True)
        print(f"  五门槛={gates} all_pass={all(gates.values())}", flush=True)

    # 基线自检（raw 须逐位复现 §10 ext:60:2.0 / §25.2）
    if "raw" in results:
        b = results["raw"]
        ok = (b["n_trades"] == 195
              and abs((b["overall"].get("mean_net_R") or 0) - 0.19007) < 2e-4
              and b["placebo"]["rand_ge_actual_pct"] == 0.0)
        print(f"\n=== raw 基线逐位复现: {'PASS' if ok else 'FAIL — 须排查'} "
              f"(n=195? {b['n_trades']}, mean_net_R≈0.19007? "
              f"{b['overall'].get('mean_net_R')}, 安慰剂=0.0%? "
              f"{b['placebo']['rand_ge_actual_pct']}) ===", flush=True)
        selfcheck = {"raw_baseline_reproduced": bool(ok),
                     "raw_n_expected": 195, "raw_mean_net_R_expected": 0.19007,
                     "raw_placebo_expected": 0.0}
    else:
        selfcheck = {"raw_baseline_reproduced": None, "note": "本批未含 raw 口径"}

    report = {
        "meta": {
            "start": str(start), "end": str(end),
            "pair_set": "base+ext", "n_pairs": len(PAIRS_ALL),
            "window": args.window, "entry_z": args.entry_z, "exit_z": args.exit_z,
            "rebalance_n": 1, "sl_mult": SL_MULT,
            "modes": modes, "n_perm": args.n_perm, "seed": args.seed,
            "v2_json": args.v2_json,
            "purpose": "七期 P0：修后口径完整重跑 §6 五门槛（补 §26.2 诚实边界）",
            "caveats": [
                "安慰剂与 §9/§10 用同一 placebo_agg 函数、同 n_perm/seed，逐位可比",
                "相关性 V2 净R字段用 'R'（V2 60m sl_atr=1.0 口径），B3 用 'net'；量纲不同不影响 Pearson",
                "相关性多基准并排(union/b3_calendar/overlap)，稀疏样本下结论为方向性，置信度中等",
                "门槛⑤阈值沿用项目惯例 |Pearson|<0.30（admit_factors.py --corr-max 默认）；门槛④ >10% 即不显著",
                "session_map 改变了 open 语义(夜盘21:00)，其漂亮数字不作生产预期",
            ],
        },
        "data_boundary": boundary,
        "modes": results,
        "selfcheck": selfcheck,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
