#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 regime 依赖性判定（C 护栏「学习型 regime 切换」的启动前置条件）。

用户约定：**待 B3 显出 regime 依赖时再启动** C 护栏。本脚本先做实证判定，不预设结论。

严谨性：
  * 分档阈值一律用**训练期(<=2023-12-31)分位数**固定，再套到全样本 → 无前视。
  * 三个 regime 维度：市场波动率(横截面平均20日波动)、趋势强度(横截面平均|20日动量|)、
    市场方向(等权指数20日动量 up/down)。
  * 分年/半年滚动稳定性；训练期 vs OOS 的「regime 特征 → 未来20日 B3 收益」Spearman 相关
    （纪律2：训练期显著但 OOS 反号/消失即假阳性，学习型切换无必要）。
  * 与 §8.9 对齐：即便显出依赖，朴素 regime 减仓已被证为**反向选择器**，任何闸须重走 WF 真盈亏校验。

口径复用 §9/§10 底座，不改 simulate。研究分支，未碰 main/cb/dev。
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
                                 simulate_windows, seg_stats, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS

TRAIN_END = date(2023, 12, 31)


def spearman(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    m = ~(np.isnan(a) | np.isnan(b))
    a, b = a[m], b[m]
    if len(a) < 5:
        return float("nan")
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return round(float(np.corrcoef(ra, rb)[0, 1]), 4)


def bucket(v, q1, q3):
    if v != v:
        return "NA"
    return "low" if v <= q1 else ("high" if v >= q3 else "mid")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--out", default="/tmp/aeq/b3_regime.json")
    args = ap.parse_args()

    pairs = list(BASE_PAIRS) + EXT_PAIRS
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    print(f"[init] n_pairs={len(pairs)} window={args.window} entry_z={args.entry_z}", flush=True)
    opens, closes, highs, lows = load_market(session, symbols,
                                             start - timedelta(days=400), end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    session.close()
    engine.dispose()
    dates = list(opens.index)
    print(f"[load] n_dates={len(dates)}", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    tgt, _ = compute_targets(closes, pairs=pairs, window=args.window,
                             entry_z=args.entry_z, exit_z=0.5,
                             return_strength=True)
    tgt = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
    trades = simulate_windows(tgt, opens, closes, atr, wfp, start, end,
                              rebalance_n=1, sl_mult=SL_MULT,
                              contract_map=contract_map, coef_cache=coef_cache)
    base = seg_stats(trades)
    print(f"[baseline] n={base['n']} 净R={base['mean_net_R']} PF={base['profit_factor']} "
          f"(应与 §10 ext:60:2.0 一致)", flush=True)

    # ---- regime 指标（date 级，横截面平均）----
    ret = closes.pct_change(fill_method=None)
    vol = ret.rolling(20, min_periods=10).std().mean(axis=1)          # 市场波动率
    mom20 = closes.pct_change(20, fill_method=None)
    trend = mom20.abs().mean(axis=1)                                    # 趋势强度
    idx_mom = ret.mean(axis=1).rolling(20, min_periods=10).sum()       # 等权指数动量(方向)

    train_mask = np.array([d <= TRAIN_END for d in dates])
    vq = (float(vol[train_mask].quantile(0.33)), float(vol[train_mask].quantile(0.67)))
    tq = (float(trend[train_mask].quantile(0.33)), float(trend[train_mask].quantile(0.67)))
    print(f"[thresholds/train<=2023] vol={vq} trend={tq}", flush=True)

    vol_b = {d: bucket(vol.at[d], *vq) for d in dates}
    trend_b = {d: bucket(trend.at[d], *tq) for d in dates}
    dirn_b = {d: ("NA" if idx_mom.at[d] != idx_mom.at[d]
                  else ("up" if idx_mom.at[d] > 0 else "down")) for d in dates}

    report = {"meta": {"start": str(start), "end": str(end), "n_pairs": len(pairs),
                       "train_end": str(TRAIN_END),
                       "vol_thresholds_train": vq, "trend_thresholds_train": tq,
                       "note": "分档阈值用训练期分位固定，无前视"},
              "baseline": base, "regime_groups": {}, "halfyear": [], "predictive": {}}

    # ---- 分组统计 ----
    for dim, buck in [("vol", vol_b), ("trend", trend_b), ("direction", dirn_b)]:
        groups = defaultdict(list)
        for t in trades:
            groups[buck.get(t["entry_dt"], "NA")].append(t)
        rows = []
        print(f"\n=== regime: {dim} ===", flush=True)
        for b in (("low", "mid", "high", "NA") if dim != "direction"
                  else ("up", "down", "NA")):
            if not groups[b]:
                continue
            s = seg_stats(groups[b])
            rows.append({"bucket": b, **s})
            print(f"  {b:5s} n={s['n']:3d} 净R={s.get('mean_net_R')} "
                  f"PF={s.get('profit_factor')} 胜率={s.get('win_rate')}% "
                  f"sumNetR={s.get('sum_net_R')}", flush=True)
        report["regime_groups"][dim] = rows

    # ---- 半年滚动 ----
    print("\n=== 半年滚动 ===", flush=True)
    hy = defaultdict(list)
    for t in trades:
        d = t["entry_dt"]
        hy[f"{d.year}H{1 if d.month <= 6 else 2}"].append(t)
    for k in sorted(hy):
        s = seg_stats(hy[k])
        report["halfyear"].append({"period": k, **s})
        print(f"  {k}: n={s['n']:3d} 净R={s.get('mean_net_R')} "
              f"PF={s.get('profit_factor')}", flush=True)

    # ---- 预测力检验：regime 特征 -> 未来20日 B3 累计净R ----
    # 日度 B3 收益记在 exit_dt（收益实现日）
    daily = defaultdict(float)
    for t in trades:
        daily[t["exit_dt"]] += (t["pnl"] - t["cost_pts"]) / t["risk"]
    b3_arr = np.array([daily.get(d, 0.0) for d in dates], dtype=float)
    fwd20 = pd.Series(b3_arr).rolling(20).sum().shift(-20).to_numpy()  # 未来20日累计
    vol_arr = vol.to_numpy(dtype=float)
    trend_arr = trend.to_numpy(dtype=float)
    oos_mask = ~train_mask
    valid = ~np.isnan(fwd20)
    print("\n=== 预测力：regime 特征 → 未来20日 B3 累计净R (Spearman) ===", flush=True)
    for name, x in [("vol", vol_arr), ("trend", trend_arr)]:
        m_tr = valid & train_mask & ~np.isnan(x)
        m_oos = valid & oos_mask & ~np.isnan(x)
        r_tr = spearman(x[m_tr], fwd20[m_tr])
        r_oos = spearman(x[m_oos], fwd20[m_oos])
        same = (r_tr == r_tr and r_oos == r_oos
                and (r_tr > 0) == (r_oos > 0) and abs(r_oos) >= 0.5 * abs(r_tr))
        report["predictive"][name] = {"train_rho": r_tr, "oos_rho": r_oos,
                                      "same_sign_and_not_decayed": same,
                                      "n_train": int(m_tr.sum()), "n_oos": int(m_oos.sum())}
        print(f"  {name}: train ρ={r_tr} (n={int(m_tr.sum())})  "
              f"oos ρ={r_oos} (n={int(m_oos.sum())})  同号且未衰减={same}", flush=True)

    # ---- 自动判定 ----
    all_grp = [r for rows in report["regime_groups"].values() for r in rows]
    n_neg = sum(1 for r in all_grp if (r.get("mean_net_R") or 0) < 0)
    n_lowpf = sum(1 for r in all_grp
                  if isinstance(r.get("profit_factor"), (int, float))
                  and r["profit_factor"] < 1.1)
    pred_ok = any(v["same_sign_and_not_decayed"] for v in report["predictive"].values())
    shows_dependence = (n_neg >= 2 or n_lowpf >= 2 or pred_ok)
    report["verdict"] = {
        "n_regime_groups": len(all_grp), "n_negative_groups": n_neg,
        "n_pf_below_1_1_groups": n_lowpf,
        "any_stable_predictive_regime": pred_ok,
        "shows_regime_dependence": shows_dependence,
        "C_guardrail": ("启动学习型切换预研" if shows_dependence
                        else "未触发：B3 未显出 regime 依赖，C 护栏维持待命"),
    }
    print("\n=== VERDICT ===", flush=True)
    print(json.dumps(report["verdict"], ensure_ascii=False, indent=1), flush=True)

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    main()
