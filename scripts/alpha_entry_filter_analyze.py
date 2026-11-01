#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Alpha 入场质量过滤 —— 门评估 + 安慰剂 + 分年稳定性 + 判定（本地，读 alpha_filter_oos.json）。

口径：所有统计基于**严格 WF-OOS 真实净 R**（毛R/成本R/净R 来自生产 run_fusion_backtest，
真实乘数 + 真实历史费率 + 加码），与 docs §8.7 基线同 footing。门均为**入场时可观测**
（side / regime / K45 段方向·进度 / ADX / ATR 分位），无前视。

核心纪律（doc §8.5 #2(c) / §8.7.7）：拒绝「少做单」安慰剂——
  ① 门须提升**逐笔 mean_net_R**（不是只降 N）；
  ② 门的保留集须**胜过安慰剂**：随机砍同样多笔 300 次，保留 sum_net_R 应 >= 随机 p50（理想 p75+）；
  ③ 须在崩塌年（2024/2025/2026）**分年稳定**，而非靠单年拉抬聚合。
判定：最佳门仍 mean_net_R<=0 或 PF<=1 稳健 → 证据指向「已衰减 old-school alpha，需 new idea」。

用法：
  python scripts/alpha_entry_filter_analyze.py [--in /tmp/alpha_filter_oos.json] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

random.seed(20261101)


def to_float(x, default=None):
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def gcn(t):
    """返回 (gross, cost, net) 或 None（风险无效）。"""
    risk = to_float(t.get("risk"), 0.0)
    if risk is None or risk <= 0:
        return None
    g = to_float(t.get("R"))
    c = to_float(t.get("cost_points"))
    n = to_float(t.get("pnl"))
    if g is None or c is None or n is None:
        return None
    return g, c / risk, n / risk


# --------------------------------------------------------------- 特征标签
def reg_favor(t):
    """顺 regime：做多且 reg==1，或做空且 reg==0。reg None → 中性。"""
    reg = t.get("reg")
    if reg is None:
        return "中性"
    up = reg > 0.5
    if t.get("side") == "LONG":
        return "顺regime" if up else "逆regime"
    return "逆regime" if up else "顺regime"


def k45_favor(t):
    d = t.get("k45_dir")
    if d is None or d == 0:
        return "中性"
    if t.get("side") == "LONG":
        return "顺K45" if d > 0 else "逆K45"
    return "逆K45" if d > 0 else "顺K45"


def prog_band(t):
    p = t.get("k45_prog")
    if p is None:
        return "无段"
    if p < 0.33:
        return "前0-33"
    if p < 0.50:
        return "中33-50"
    if p <= 0.83:
        return "后50-83"
    return "末>83"


# --------------------------------------------------------------- 门（keep 谓词）
def keep_base(t):
    return True


def keep_long_only(t):
    return t.get("side") == "LONG"


def keep_s_regime_rev(t):
    return not (t.get("side") == "SHORT" and reg_favor(t) == "逆regime")


def keep_s_k45_rev(t):
    return not (t.get("side") == "SHORT" and k45_favor(t) == "逆K45")


def keep_s_rev_union(t):
    return not (t.get("side") == "SHORT"
                and (reg_favor(t) == "逆regime" or k45_favor(t) == "逆K45"))


def keep_mid_stop(t):
    return prog_band(t) != "中33-50"


def keep_end_stop(t):
    return prog_band(t) != "末>83"


def keep_regime_rev(t):
    return reg_favor(t) != "逆regime"


def keep_c_final(t):
    return keep_mid_stop(t) and keep_s_rev_union(t)


def keep_c_final_v2(t):
    return keep_mid_stop(t) and keep_s_regime_rev(t)


def keep_adx_ge25(t):
    a = t.get("adx")
    return a is None or a >= 25.0


def keep_lowvol(t):
    q = t.get("atr_pctile")
    return q is None or q <= 0.8


GATES = {
    "base": keep_base,
    "LONG_only": keep_long_only,
    "S_regime_rev": keep_s_regime_rev,
    "S_k45_rev": keep_s_k45_rev,
    "S_rev_union": keep_s_rev_union,
    "K45_mid_stop": keep_mid_stop,
    "K45_end_stop": keep_end_stop,
    "regime_rev_all": keep_regime_rev,
    "C_final": keep_c_final,
    "C_final_v2": keep_c_final_v2,
    "ADX_ge25": keep_adx_ge25,
    "lowvol_le80": keep_lowvol,
}


# --------------------------------------------------------------- 统计
def seg_stats(rows):
    if not rows:
        return {"n": 0}
    g = np.array([r[0] for r in rows]); c = np.array([r[1] for r in rows])
    n = np.array([r[2] for r in rows])
    wins = n > 0; losses = n < 0
    sum_win = float(n[wins].sum()) if wins.any() else 0.0
    sum_loss = float(n[losses].sum()) if losses.any() else 0.0
    pf = (sum_win / abs(sum_loss)) if sum_loss != 0 else (float("inf") if sum_win > 0 else 0.0)
    le_cost = float((g <= c).mean()) if len(rows) else 0.0
    return {
        "n": int(len(rows)),
        "win_rate": round(float(wins.mean()) * 100, 2),
        "mean_gross_R": round(float(g.mean()), 5),
        "mean_cost_R": round(float(c.mean()), 5),
        "mean_net_R": round(float(n.mean()), 5),
        "sum_gross_R": round(float(g.sum()), 1),
        "sum_cost_R": round(float(c.sum()), 1),
        "sum_net_R": round(float(n.sum()), 1),
        "profit_factor": (round(pf, 3) if pf != float("inf") else "inf"),
        "gross_le_cost_pct": round(le_cost * 100, 2),
    }


def fast_cluster_pct(rows):
    if not rows:
        return None
    return round(float(np.mean([(t.get("holding_bars", 0) or 0) <= 10
                                for _, _, _, t in rows])) * 100, 2)


# rows 元素为 (g, c, n, t)
def apply_gate(rows, keep_fn):
    kept = [(g, c, n, t) for (g, c, n, t) in rows if keep_fn(t)]
    return kept


def placebo(rows, kept_idxs, n_iter=300):
    """随机砍同样多笔，比较保留 sum_net_R 分布。rows: list of (g,c,n,t)。"""
    all_n = [n for (_, _, n, _) in rows]
    k_cut = len(rows) - len(kept_idxs)
    actual_kept_sum = sum(all_n[i] for i in kept_idxs)
    rng = random.Random(20261101)
    sums = []
    idxs_all = list(range(len(rows)))
    for _ in range(n_iter):
        drop = set(rng.sample(idxs_all, min(k_cut, len(rows))))
        s = sum(all_n[i] for i in idxs_all if i not in drop)
        sums.append(s)
    sums.sort()
    q = lambda p: round(float(sums[int(p * (len(sums) - 1))]), 1)
    better = sum(1 for s in sums if s >= actual_kept_sum)
    return {
        "cut_n": int(k_cut),
        "actual_kept_sum_net_R": round(float(actual_kept_sum), 1),
        "rand_p05": q(0.05), "rand_p50": q(0.50), "rand_p95": q(0.95),
        "rand_ge_actual_pct": round(100.0 * better / len(sums), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="/tmp/alpha_filter_oos.json")
    ap.add_argument("--out", default="scripts/_alpha_filter_report.json")
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    raw = data["rows"]
    # 构造 (g,c,n,t)
    rows = []
    for t in raw:
        gcnv = gcn(t)
        if gcnv is None:
            continue
        rows.append((gcnv[0], gcnv[1], gcnv[2], t))
    print(f"有效成交（风险有效）{len(rows)} 笔 / 原始 {len(raw)} 笔")

    base_stats = seg_stats(rows)
    base_fast = fast_cluster_pct(rows)
    print(f"[base] n={base_stats['n']} 胜率={base_stats['win_rate']}% "
          f"毛R={base_stats['mean_gross_R']} 成本R={base_stats['mean_cost_R']} "
          f"净R={base_stats['mean_net_R']} PF={base_stats['profit_factor']} "
          f"快亏集群(<=10根)={base_fast}%")

    report = {"meta": data.get("meta", {}),
              "base": {**base_stats, "fast_cluster_pct": base_fast}}
    report["gates"] = {}
    report["by_year"] = {}

    # 分年
    yr_all = defaultdict(list)
    for r in rows:
        d = r[3].get("entry_date")
        if d:
            yr_all[d[:4]].append(r)

    for gname, fn in GATES.items():
        kept = apply_gate(rows, fn)
        kept_idxs = [i for i, r in enumerate(rows) if fn(r[3])]
        st = seg_stats(kept)
        st["fast_cluster_pct"] = fast_cluster_pct(kept)
        pl = placebo(rows, kept_idxs)
        # 相对 base 的逐笔净R变化
        delta = (round(st["mean_net_R"] - base_stats["mean_net_R"], 5)
                 if st.get("n", 0) > 0 else None)
        st["delta_mean_net_R_vs_base"] = delta
        st["n_drop_pct"] = round((1 - st["n"] / base_stats["n"]) * 100, 2) if base_stats["n"] else None
        st["placebo"] = pl
        report["gates"][gname] = st
        # 分年
        yrk = defaultdict(list)
        for r in kept:
            d = r[3].get("entry_date")
            if d:
                yrk[d[:4]].append(r)
        report["by_year"][gname] = {y: seg_stats(v) for y, v in sorted(yrk.items())}

    # 控制台
    print("\n========== 门评估（WF-OOS 真实净 R）==========")
    hdr = f"{'gate':16} {'n':>6} {'drop%':>6} {'win%':>6} {'净R':>8} {'Δ净R':>8} {'PF':>6} {'快亏%':>6} {'pl_p50':>9} {'pl_ge%':>7}"
    print(hdr)
    for gname, st in report["gates"].items():
        if st.get("n", 0) == 0:
            continue
        pl = st["placebo"]
        print(f"{gname:16} {st['n']:>6} {str(st['n_drop_pct']):>6} {st['win_rate']:>6} "
              f"{st['mean_net_R']:>8} {str(st['delta_mean_net_R_vs_base']):>8} "
              f"{str(st['profit_factor']):>6} {str(st['fast_cluster_pct']):>6} "
              f"{pl['rand_p50']:>9} {pl['rand_ge_actual_pct']:>7}")

    # 判定：对最佳候选（C_final / C_final_v2 / S_rev_union / regime_rev_all）做分年稳定评估
    print("\n========== 分年稳定性（崩塌年 2024/2025/2026 的 mean_net_R）==========")
    for gname in ("C_final", "C_final_v2", "S_rev_union", "regime_rev_all", "LONG_only"):
        by = report["by_year"].get(gname, {})
        line = f"{gname:16}: "
        for y in ("2024", "2025", "2026"):
            s = by.get(y)
            line += f"{y}={s['mean_net_R'] if s and s.get('n') else 'NA':>8} "
        print(line)

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                             encoding="utf-8")
    print(f"\n报告已写 {args.out}")


if __name__ == "__main__":
    main()
