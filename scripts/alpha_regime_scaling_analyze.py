#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regime 自适应减仓 —— 方案评估 + 安慰剂 + 权益曲线 + 分年（2026-11-01，研究分支）。

输入：alpha_regime_scaling_oos.py 产出的富集成交 JSON（含市场级/品种级 ex-ante 特征）。

核心方法论（因逐笔 net 是 per-unit R，对仓位尺度不变）：
  减仓改变的是**组合暴露**，必须用**聚合量**评估，不能看逐笔均值。
  - 每方案对每笔给尺度 s_i ∈ [0,1]；agg_net = Σ s_i·net_i（基线 base：s=1）；
  - 权益曲线：按平仓序累积 Σ s_i·net_i，算 Sharpe_proxy（按月聚合 net R）与最大回撤；
  - 暴露保留 = Σ s_i / N；
  - 分年 Σ s_i·net_i（直接回答「是否救援 2025」）；
  - 安慰剂：标签置换（随机指定同样数量的「坏 regime」成交，300 次）→ pct=置换≥真实的比例；
    若 pct 低（<5%）→ regime 时机真有信息量，非「少做单」安慰剂。

判定：
  Real    = pct_perm < 5%；
  Helps   = agg_net_scheme > agg_net_base（聚合减亏/增盈）；
  Rescue  = 方案下 2025 年 agg_net > 0 或 PF(Σwin/|Σloss|) >= 1；
  最终：若 Real+Helps 但不 Rescue 2025 → 仅风险闸（减 bleed），非修复；
        若无人 Rescue 2025 → 结构衰减确认，减仓只修边幅，需 new idea。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

BARS_PER_DAY = 6.7
RNG = np.random.default_rng(20261101)


def parse_dt(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    try:
        return datetime.fromisoformat(str(x))
    except Exception:
        return None


def load_rows(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d["meta"], d["rows"]


def get_feat(r, k):
    v = r.get(k)
    return float(v) if v is not None else None


# ---------------------------------------------------------------- 方案定义
# 每个方案：(predicate(row)->bool 是否「坏 regime」, 坏时尺度 s_bad)
def make_schemes():
    def reg_flat(r):
        v = get_feat(r, "reg_cont")
        return (v is not None) and v < 1.0
    def adx_flat(r):
        v = get_feat(r, "mkt_adx")
        return (v is not None) and v < 20.0
    def vol_flat(r):
        v = get_feat(r, "vol_pct")
        return (v is not None) and v > 0.80
    def mom_flat(r):
        v = get_feat(r, "mom")
        return (v is not None) and abs(v) < 0.10
    def combo_flat(r):
        return reg_flat(r) or adx_flat(r) or vol_flat(r)
    def asset_adx_half(r):
        v = get_feat(r, "adx")
        return (v is not None) and v < 20.0

    S = {
        "base":          (lambda r: False, 1.0),
        "S_reg_flat":    (reg_flat, 0.0),
        "S_reg_half":    (reg_flat, 0.5),
        "S_adx_flat":    (adx_flat, 0.0),
        "S_adx_half":    (adx_flat, 0.5),
        "S_vol_flat":    (vol_flat, 0.0),
        "S_vol_half":    (vol_flat, 0.5),
        "S_mom_flat":    (mom_flat, 0.0),
        "S_mom_half":    (mom_flat, 0.5),
        "S_combo_flat":  (combo_flat, 0.0),
        "S_combo_half":  (combo_flat, 0.5),
        "S_asset_adx_half": (asset_adx_half, 0.5),
    }
    return S


# ---------------------------------------------------------------- 指标
def equity_curve_stats(scales, nets, exit_dts):
    """按平仓序累积，返回 (agg_net, exposure, sharpe_proxy, max_dd)。"""
    order = np.argsort(exit_dts)
    s = scales[order]; n = nets[order]
    agg = float((s * n).sum())
    exposure = float(s.mean())
    cum = np.cumsum(s * n)
    # 最大回撤
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(dd.max())
    # 按月聚合 net R → Sharpe_proxy（年化）
    months = {}
    for dt, v in zip(exit_dts[order], (s * n)):
        key = (dt.year, dt.month)
        months[key] = months.get(key, 0.0) + float(v)
    mv = np.array(list(months.values()), dtype=float)
    if mv.std() > 1e-9 and len(mv) > 2:
        sharpe = float(mv.mean() / mv.std() * np.sqrt(12))
    else:
        sharpe = 0.0
    return agg, exposure, sharpe, max_dd


def by_year_agg(scales, nets, years):
    out = {}
    for y in sorted(set(years)):
        m = years == y
        out[str(y)] = {
            "agg_net": round(float((scales[m] * nets[m]).sum()), 1),
            "n": int(m.sum()),
        }
    return out


def pf_of(agg_win, agg_loss):
    if agg_loss == 0:
        return float("inf") if agg_win > 0 else 0.0
    return agg_win / abs(agg_loss)


def permutation_placebo(scales_base, nets, bad_mask, s_bad, n_iter=300):
    """保持「坏 regime 数量」与 s_bad 不变，随机指定哪些成交被减仓，重算 agg_net。
    返回 (pct_ge_real, mean_perm, std_perm, real_agg)。"""
    N = len(nets)
    real = float((scales_base * nets).sum())
    n_bad = int(bad_mask.sum())
    if n_bad == 0 or n_bad == N:
        return 100.0, real, 0.0, real
    perms = np.empty(n_iter)
    idx_all = np.arange(N)
    for k in range(n_iter):
        pick = RNG.choice(idx_all, size=n_bad, replace=False)
        sc = np.ones(N)
        sc[pick] = s_bad
        perms[k] = float((sc * nets).sum())
    pct = float((perms >= real).mean()) * 100.0
    return round(pct, 1), round(float(perms.mean()), 1), round(float(perms.std()), 1), round(real, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="/tmp/alpha_regime_report.json")
    ap.add_argument("--n-perm", type=int, default=300)
    args = ap.parse_args()

    meta, rows = load_rows(args.inp)
    # 解析：成交已按「逐手(lot)」拆开，pnl/cost_points/risk 均为每手口径；
    # net = pnl/risk（每手 per-unit 净 R，对仓位尺度不变），减仓以 s_i 缩放每手贡献。
    def derive(r):
        risk = float(r.get("risk") or 0.0)
        if not (risk > 0):
            return None
        net = float(r.get("pnl") or 0.0) / risk
        gross = float(r.get("R") or 0.0)
        cost = float(r.get("cost_points") or 0.0) / risk
        return gross, cost, net
    parsed = [derive(r) for r in rows]
    keep = [i for i, p in enumerate(parsed) if p is not None]
    rows = [rows[i] for i in keep]
    gross = np.array([parsed[i][0] for i in keep], dtype=float)
    cost = np.array([parsed[i][1] for i in keep], dtype=float)
    net = np.array([parsed[i][2] for i in keep], dtype=float)
    exit_dt = np.array([parse_dt(r.get("exit_dt")) or parse_dt(r.get("entry_dt"))
                        for r in rows])
    exit_dt = np.array([(dt if dt is not None else datetime(2000, 1, 1))
                        for dt in exit_dt])
    years = np.array([dt.year for dt in exit_dt])

    S = make_schemes()
    report = {"meta": meta, "schemes": {}, "regime_conditional": {}, "verdict": {}}

    # 先算 base
    base_scales = np.ones(len(rows))
    base_agg, base_exp, base_sh, base_dd = equity_curve_stats(base_scales, net, exit_dt)
    base_by_year = by_year_agg(base_scales, net, years)
    report["base"] = {
        "agg_net": round(base_agg, 1), "exposure": round(base_exp, 3),
        "sharpe_proxy": round(base_sh, 3), "max_dd": round(base_dd, 1),
        "by_year": base_by_year,
    }

    for name, (pred, s_bad) in S.items():
        if name == "base":
            continue
        bad = np.array([pred(r) for r in rows], dtype=bool)
        scales = np.where(bad, s_bad, 1.0)
        agg, exp, sh, dd = equity_curve_stats(scales, net, exit_dt)
        by_y = by_year_agg(scales, net, years)
        # 2025 救援判定
        m25 = years == 2025
        win25 = (scales[m25] * net[m25]) > 0
        agg_win25 = float((scales[m25] * net[m25])[win25].sum()) if win25.any() else 0.0
        agg_loss25 = float((scales[m25] * net[m25])[~win25].sum()) if (~win25).any() else 0.0
        pf25 = pf_of(agg_win25, agg_loss25)
        # 安慰剂
        pct, pmean, pstd, preal = permutation_placebo(base_scales, net, bad, s_bad, args.n_perm)
        report["schemes"][name] = {
            "s_bad": s_bad,
            "n_bad": int(bad.sum()),
            "n_bad_pct": round(float(bad.mean()) * 100, 1),
            "agg_net": round(agg, 1),
            "delta_agg_vs_base": round(agg - base_agg, 1),
            "exposure": round(exp, 3),
            "sharpe_proxy": round(sh, 3),
            "delta_sharpe_vs_base": round(sh - base_sh, 3),
            "max_dd": round(dd, 1),
            "delta_dd_vs_base": round(dd - base_dd, 1),
            "by_year": by_y,
            "y2025_agg_net": round(float((scales[m25] * net[m25]).sum()), 1),
            "y2025_PF": (round(pf25, 3) if pf25 != float("inf") else "inf"),
            "y2025_rescued": bool((scales[m25] * net[m25]).sum() > 0 or (pf25 >= 1)),
            "placebo_pct_ge_real": pct,
            "placebo_mean": pmean,
            "placebo_std": pstd,
            "is_real": bool(pct < 5.0),
            "helps_agg": bool(agg > base_agg),
        }

    # regime 条件表现表：每个市场指标分「坏/好」桶，看坏桶是否真更差（减仓前提）
    cond_defs = {
        "reg_cont<1": lambda r: (get_feat(r, "reg_cont") is not None) and get_feat(r, "reg_cont") < 1.0,
        "mkt_adx<20": lambda r: (get_feat(r, "mkt_adx") is not None) and get_feat(r, "mkt_adx") < 20.0,
        "vol_pct>0.8": lambda r: (get_feat(r, "vol_pct") is not None) and get_feat(r, "vol_pct") > 0.80,
        "abs(mom)<0.1": lambda r: (get_feat(r, "mom") is not None) and abs(get_feat(r, "mom")) < 0.10,
    }
    for cname, cpred in cond_defs.items():
        bad = np.array([cpred(r) for r in rows], dtype=bool)
        good = ~bad
        def bucket(m):
            if m.sum() == 0:
                return {"n": 0}
            return {
                "n": int(m.sum()),
                "mean_net": round(float(net[m].mean()), 4),
                "agg_net": round(float(net[m].sum()), 1),
                "win_rate": round(float((net[m] > 0).mean()) * 100, 1),
            }
        report["regime_conditional"][cname] = {
            "bad": bucket(bad), "good": bucket(good),
        }

    # 判决汇总
    schemes = report["schemes"]
    real_help = [k for k, v in schemes.items() if v["is_real"] and v["helps_agg"]]
    rescuers = [k for k, v in schemes.items() if v.get("y2025_rescued")]
    report["verdict"] = {
        "base_agg_net": round(base_agg, 1),
        "real_and_helps": real_help,
        "y2025_rescuers": rescuers,
        "best_by_delta_agg": max(schemes.items(), key=lambda kv: kv[1]["delta_agg_vs_base"])[0]
            if schemes else None,
        "conclusion": (
            "结构衰减确认：无方案救援 2025（减仓只减 bleed，非修复）；需 new idea"
            if not rescuers else
            "存在可救援 2025 的方案，值得落地"
        ),
    }

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")

    # ---------------- 控制台摘要 ----------------
    print("========== Regime 自适应减仓评估 ==========")
    print(f"样本 {len(rows)} 笔；base agg_net={base_agg:.1f} Sharpe={base_sh:.3f} "
          f"maxDD={base_dd:.1f}")
    print(f"\n{'方案':16}{'坏%':>6}{'agg_net':>9}{'Δagg':>8}{'暴露':>7}"
          f"{'Sharpe':>8}{'ΔDD':>8}{'2025':>9}{'PF25':>7}{'安慰剂%':>8} Real")
    for k, v in schemes.items():
        print(f"{k:16}{v['n_bad_pct']:>6}{v['agg_net']:>9}{v['delta_agg_vs_base']:>8}"
              f"{v['exposure']:>7}{v['sharpe_proxy']:>8}{v['delta_dd_vs_base']:>8}"
              f"{v['y2025_agg_net']:>9}{str(v['y2025_PF']):>7}{v['placebo_pct_ge_real']:>8}"
              f" {v['is_real']}")
    print("\n-- regime 条件表现（坏桶是否真更差）--")
    for c, b in report["regime_conditional"].items():
        print(f"  {c:14} 坏: n={b['bad']['n']} meanNet={b['bad']['mean_net']} "
              f"agg={b['bad']['agg_net']} 胜率={b['bad']['win_rate']}% | "
              f"好: n={b['good']['n']} meanNet={b['good']['mean_net']} "
              f"agg={b['good']['agg_net']} 胜率={b['good']['win_rate']}%")
    print("\n-- 判决 --")
    print(f"  Real+Helps: {real_help}")
    print(f"  2025 救援者: {rescuers}")
    print(f"  结论: {report['verdict']['conclusion']}")
    print(f"\n结果已写 {args.out}")


if __name__ == "__main__":
    main()
