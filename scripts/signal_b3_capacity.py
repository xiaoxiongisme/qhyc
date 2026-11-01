#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 容量梯度 / 加仓规则 / 冲击成本复核（§9.7 待办3；研究分支，未碰 main/cb/dev）。

口径说明（纪律5/9，务必先读）：
  * 底座 simulate 输出 R 口径：每笔 net_R = (pnl - cost) / risk，risk = sl_mult × 日线 ATR。
    **R 口径下同比例放大仓位不改变单笔 R**（分子分母同乘 size），故加仓效果必须在
    **金额口径**下看。本脚本所有变体都在底座原始成交上**事后重算**，不改 simulate。
  * 金额口径用 **entry_px 归一收益率** pnl/entry_px（跨品种可比）；同时保留原始点数加总，
    但注明「不同合约乘数/点值不同，跨对原始点数加总有量纲失真，仅作同对内参考」。
  * 容量梯度 K∈{3,5,7,10,13 对}：正确口径 = **持仓感知、只限制新开仓**——已持仓的对
    继续持有直到信号自然平仓，仅当持仓对未达 K 时才允许新对按当日 |z| 强度补位。
    （初版「每日 top-K 重排」会强制平掉掉出 top-K 的持仓再开回，制造巨额换手、边被成本吃光，
     属错误口径；本脚本保留为 capacity_naive 对照，见 §11。）
  * 冲击成本压力：每笔 cost_pts 整体乘 mult∈{1,2,3}（含手续费与滑点的整体加压，偏保守；
    滑点原假设 1 跳/边），重算 net_R = (pnl - cost×mult)/risk。
  * 加仓规则：建仓时 |z|>3 → size=2，否则 size=1；归一收益率与成本同乘 size。
  * MDD：逐笔按 entry_dt 排序后累计曲线的最大回撤（R 口径 mdd_R / 归一收益率 mdd_return）。

用法：python signal_b3_capacity.py --pair-set ext --window 60 --entry-z 2.0
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS

CAPS = (3, 5, 7, 10, 13)
COST_MULTS = (1.0, 2.0, 3.0)


def mdd(series):
    """给定顺序的累计曲线最大回撤。"""
    if len(series) == 0:
        return 0.0
    eq = np.cumsum(np.asarray(series, dtype=float))
    peak = np.maximum.accumulate(eq)
    return round(float((peak - eq).max()), 5)


def r_stats(trades, cost_mult: float = 1.0):
    """R 口径（与底座一致，仅把逐笔成本整体乘 cost_mult 做压力测试）。按 entry_dt 排序算 MDD。"""
    if not trades:
        return {"n": 0}
    ts = sorted(trades, key=lambda t: t["entry_dt"])
    net = np.array([(t["pnl"] - t["cost_pts"] * cost_mult) / t["risk"] for t in ts])
    wins, losses = net > 0, net < 0
    sw = float(net[wins].sum()) if wins.any() else 0.0
    sl = float(net[losses].sum()) if losses.any() else 0.0
    pf = sw / abs(sl) if sl != 0 else (float("inf") if sw > 0 else 0.0)
    return {"n": len(ts), "mean_net_R": round(float(net.mean()), 5),
            "sum_net_R": round(float(net.sum()), 2),
            "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            "win_rate": round(float(wins.mean()) * 100, 2),
            "mdd_R": mdd(net)}


def amount_stats(trades, size_fn=None, cost_mult: float = 1.0):
    """金额口径：用 entry_px 归一收益率，跨品种可比。原始点数另行保留（注明量纲限制）。"""
    if not trades:
        return {"n": 0}
    ts = sorted(trades, key=lambda t: t["entry_dt"])
    sizes = np.array([size_fn(t) if size_fn else 1 for t in ts], dtype=float)
    ret = np.array([t["pnl"] / t["entry_px"] for t in ts]) * sizes
    cost_ret = np.array([t["cost_pts"] / t["entry_px"] for t in ts]) * sizes * cost_mult
    net = ret - cost_ret
    wins, losses = net > 0, net < 0
    sw = float(net[wins].sum()) if wins.any() else 0.0
    sl = float(net[losses].sum()) if losses.any() else 0.0
    pf = sw / abs(sl) if sl != 0 else (float("inf") if sw > 0 else 0.0)
    # 原始点数（跨品种量纲不同，仅作参考）
    pnl_pts = np.array([t["pnl"] for t in ts]) * sizes
    cost_pts = np.array([t["cost_pts"] for t in ts]) * sizes * cost_mult
    return {"n": len(ts),
            "sum_net_return": round(float(net.sum()), 5),          # 无量纲，×100 即 %
            "sum_pnl_points": round(float(pnl_pts.sum()), 1),      # 仅同对内可比
            "sum_cost_points": round(float(cost_pts.sum()), 1),
            "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            "mdd_return": mdd(net),
            "size2_trades": int((sizes == 2).sum())}


def pair_of(symbol: str, pairs):
    v = symbol[:-3] if symbol.endswith("888") else symbol
    for va, vb in pairs:
        if v == va or v == vb:
            return f"{va}-{vb}"
    return v


def cap_targets_aware(targets, strength, pairs, K):
    """正确容量口径：只限制**新开仓**。已持仓的对继续持有直到信号自然平仓；
    仅当持仓对数未达 K 时，才按当日 |z| 强度让新对补位。不强制平仓、不人为制造换手。"""
    out = targets.copy()
    src = targets
    cols = set(targets.columns)
    prev_active = set()
    for d in src.index:
        intent = {}
        for idx, (va, vb) in enumerate(pairs):
            sa = va + "888"
            if sa in src.columns and abs(src.at[d, sa]) > 0:
                z = strength.at[d, sa] if sa in strength.columns else float("nan")
                intent[idx] = float(z) if z == z else 0.0
        held = {i for i in prev_active if i in intent}   # 已持有且今日仍有意持仓
        new_cands = sorted(((intent[i], i) for i in intent if i not in held), reverse=True)
        slots = max(0, K - len(held))
        chosen = held | {i for _, i in new_cands[:slots]}
        for idx, (va, vb) in enumerate(pairs):
            if idx not in chosen:
                sa, sb = va + "888", vb + "888"
                if sa in cols:
                    out.at[d, sa] = 0.0
                if sb in cols:
                    out.at[d, sb] = 0.0
        prev_active = chosen
    return out


def cap_targets_naive(targets, strength, pairs, K):
    """[错误口径·对照] 每日无条件按 |z| 选 top-K 对，其余置 0 → 强制平掉掉出 top-K 的持仓
    再开回，换手爆炸。仅用于证明「容量口径选错会得出完全相反的结论」。"""
    out = targets.copy()
    cols = set(targets.columns)
    for d in targets.index:
        scored = []
        for va, vb in pairs:
            sa, sb = va + "888", vb + "888"
            if sa in strength.columns:
                z = strength.at[d, sa]
                if z == z:
                    scored.append((float(z), sa, sb))
        scored.sort(reverse=True)
        keep = set()
        for _, sa, sb in scored[:K]:
            keep.add(sa)
            keep.add(sb)
        for va, vb in pairs:
            sa, sb = va + "888", vb + "888"
            if sa in cols and sa not in keep:
                out.at[d, sa] = 0.0
                if sb in cols:
                    out.at[d, sb] = 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--pair-set", choices=["base", "ext"], default="ext")
    ap.add_argument("--out", default="/tmp/aeq/b3_capacity.json")
    args = ap.parse_args()

    pairs = list(BASE_PAIRS) if args.pair_set == "base" else list(BASE_PAIRS) + EXT_PAIRS
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print(f"[init] pair_set={args.pair_set} n_pairs={len(pairs)} window={args.window} "
          f"entry_z={args.entry_z}", flush=True)
    opens, closes, highs, lows = load_market(session, symbols, start - timedelta(days=400), end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    session.close()
    engine.dispose()
    print(f"[load] n_dates={len(opens.index)}", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    raw_t, raw_s = compute_targets(closes, pairs=pairs, window=args.window,
                                   entry_z=args.entry_z, exit_z=args.exit_z,
                                   return_strength=True)
    raw_s = raw_s.reindex(index=opens.index, columns=symbols)
    entry_strength = {(d, c): raw_s.at[d, c] for d in raw_s.index for c in raw_s.columns}

    def size_fn(t):
        z = entry_strength.get((t["entry_dt"], t["symbol"]), float("nan"))
        return 2 if (z == z and z > 3.0) else 1

    def run(tgt):
        t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
        return simulate_windows(t, opens, closes, atr, wfp, start, end,
                                rebalance_n=1, sl_mult=SL_MULT,
                                contract_map=contract_map, coef_cache=coef_cache)

    # ---- 基线校验点（须与 §10 ext:60:2.0 逐位一致）----
    base_trades = run(raw_t)
    b0 = r_stats(base_trades)
    print(f"[baseline] n={b0['n']} 净R={b0['mean_net_R']} PF={b0['profit_factor']} "
          f"(应与 §10 ext:60:2.0 n=195/0.19007/1.734 一致)", flush=True)

    report = {
        "meta": {"start": str(start), "end": str(end), "pair_set": args.pair_set,
                 "n_pairs": len(pairs), "window": args.window, "entry_z": args.entry_z,
                 "exit_z": args.exit_z, "caps": list(CAPS), "cost_mults": list(COST_MULTS),
                 "sizing_rule": "size=2 if |z| at entry > 3.0 else 1",
                 "capacity_rule": "持仓感知、只限制新开仓；已持仓对自然平仓，按|z|强度补位至K",
                 "amount_rule": "归一收益率 pnl/entry_px（跨品种可比）；原始点数仅同对内可比",
                 "cost_rule": "逐笔 cost_pts 整体乘 mult（含手续费+滑点的整体加压，偏保守；滑点原假设1跳/边）"},
        "baseline": b0, "capacity": [], "capacity_naive": [],
        "cost_stress": [], "sizing": {}, "by_pair": [],
    }

    # ---- 容量梯度（正确口径）----
    print("\n=== 容量梯度 K（持仓感知，只限制新开仓）===", flush=True)
    prev_sum = None
    for K in CAPS:
        tr = run(cap_targets_aware(raw_t, raw_s, pairs, K))
        rs = r_stats(tr)
        am = amount_stats(tr)
        by_year = defaultdict(list)
        for t in tr:
            by_year[_dt(t["entry_dt"]).year].append(t)
        rec = {"K": K, "r": rs, "amount": am,
               "by_year_netR": {str(y): r_stats(v)["mean_net_R"]
                                for y, v in sorted(by_year.items())}}
        report["capacity"].append(rec)
        delta = "" if prev_sum is None else f"  ΔsumNetR={rs['sum_net_R'] - prev_sum:+.2f}"
        print(f"  K={K:2d} n={rs['n']:3d} 净R={rs['mean_net_R']} PF={rs['profit_factor']} "
              f"sumNetR={rs['sum_net_R']} mdd_R={rs['mdd_R']} "
              f"归一收益={am['sum_net_return']}{delta}", flush=True)
        prev_sum = rs["sum_net_R"]

    # ---- 对照：错误口径（证明容量判定对口径极敏感）----
    print("\n=== [对照] naive 每日 top-K 重排（强制平仓、换手爆炸，错误口径）===", flush=True)
    for K in CAPS:
        tr = run(cap_targets_naive(raw_t, raw_s, pairs, K))
        rs = r_stats(tr)
        report["capacity_naive"].append({"K": K, "r": rs})
        print(f"  K={K:2d} n={rs['n']:4d} 净R={rs['mean_net_R']} PF={rs['profit_factor']} "
              f"sumNetR={rs['sum_net_R']}", flush=True)

    # ---- 冲击成本压力 ----
    print("\n=== 冲击成本压力 (cost_mult，基线全配对) ===", flush=True)
    for m in COST_MULTS:
        rs = r_stats(base_trades, cost_mult=m)
        am = amount_stats(base_trades, cost_mult=m)
        report["cost_stress"].append({"cost_mult": m, "r": rs, "amount": am})
        print(f"  mult={m} 净R={rs['mean_net_R']} PF={rs['profit_factor']} "
              f"sumNetR={rs['sum_net_R']} 归一收益={am['sum_net_return']}", flush=True)

    # ---- 加仓规则（归一收益率口径）----
    print("\n=== 加仓规则 (|z|>3 -> size2) ===", flush=True)
    b1 = amount_stats(base_trades)
    b2 = amount_stats(base_trades, size_fn=size_fn)
    report["sizing"] = {"flat_size1": b1, "z_gt3_size2": b2}
    print(f"  size1      : 归一收益={b1['sum_net_return']} PF={b1['profit_factor']} "
          f"MDD={b1['mdd_return']}", flush=True)
    print(f"  size2@|z|>3: 归一收益={b2['sum_net_return']} PF={b2['profit_factor']} "
          f"MDD={b2['mdd_return']} 加倍笔数={b2['size2_trades']}", flush=True)

    # ---- 逐对复核（归一收益率，避免跨品种点数失真）----
    print("\n=== 逐对（归一收益率 / 成本占比 / 净R）===", flush=True)
    by_pair = defaultdict(list)
    for t in base_trades:
        by_pair[pair_of(t["symbol"], pairs)].append(t)
    for p, tr in sorted(by_pair.items()):
        rs = r_stats(tr)
        am = amount_stats(tr)
        cost_ratio = (round(am["sum_cost_points"] / abs(am["sum_pnl_points"]) * 100, 2)
                      if am["sum_pnl_points"] else None)
        rec = {"pair": p, "n": rs["n"], "mean_net_R": rs.get("mean_net_R"),
               "sum_net_R": rs.get("sum_net_R"),
               "PF": rs.get("profit_factor"), "sum_net_return": am["sum_net_return"],
               "sum_pnl_points": am["sum_pnl_points"],
               "sum_cost_points": am["sum_cost_points"], "cost_over_gross_pct": cost_ratio}
        report["by_pair"].append(rec)
        print(f"  {p:10s} n={rs['n']:3d} 净R={rs.get('mean_net_R')} "
              f"PF={rs.get('profit_factor')} 归一收益={am['sum_net_return']} "
              f"成本/毛利={cost_ratio}%", flush=True)

    # ---- 事后观察（描述性，非准入结论）：剔除成本占比>50%的薄毛利对 ----
    bad = [r["pair"] for r in report["by_pair"]
           if (r["cost_over_gross_pct"] or 0) > 50]
    if bad:
        bad_set = set(bad)
        keep_pairs = [(va, vb) for va, vb in pairs if f"{va}-{vb}" not in bad_set]
        t2, _ = compute_targets(closes, pairs=keep_pairs, window=args.window,
                                entry_z=args.entry_z, exit_z=args.exit_z,
                                return_strength=True)
        rs2 = r_stats(run(t2))
        am2 = amount_stats(run(t2))
        report["posthoc_drop_thin"] = {
            "dropped": bad, "n_pairs_kept": len(keep_pairs), "r": rs2, "amount": am2,
            "note": "事后观察：剔除理由含全样本成本占比，存在选择偏差；仅供下一步"
                    "「训练期先验剔除 + OOS 验证」，不作本次准入依据"}
        print(f"\n=== [事后观察·含选择偏差] 剔除薄毛利对 {bad} ===", flush=True)
        print(f"  保留 {len(keep_pairs)} 对: n={rs2['n']} 净R={rs2['mean_net_R']} "
              f"PF={rs2['profit_factor']} 归一收益={am2['sum_net_return']} "
              f"(基线 净R=0.19007 / 归一=1.89933)", flush=True)

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
