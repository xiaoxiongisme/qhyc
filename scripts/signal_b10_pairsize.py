"""十期③ 配仓层「最小可交易规模」约束（按对过滤，训练期定 k、OOS 验证）

问题（§30 R4）：2026 年"全 frac 档全亏"主因是 1 手地板 —— SC888 单笔 1 手风险达
10–20% 权益（目标 1.67%）。此前 §30 的反事实是**按腿**过滤，会造出裸露腿，不可执行。
本脚本按**对**过滤：同一 (entry_dt, pair) 组内任一条腿 1 手风险 > k×目标风险 → 整对跳过。

预注册（跑之前写死，避免事后挑 k）：
  网格 k ∈ {1.5, 2, 3, 5, 8, ∞}
  目标风险 = frac × E0（frac=0.02，九期修正口径）
  选择规则：k* = argmax 训练期(2021-2023) NAV，约束训练期 MDD ≤ 0.25；并列取较小 k
  验证：OOS(2024-2026) 报 NAV / MDD / 成交对 / 跳过对
对照（区分"规模匹配"与"剔除亏损单"）：
  C1 E0=1e8（资金放大 100 倍 → 1 手地板消失，若过滤增益随之消失则支持"规模错配"解释）
  C2 随机跳过同数量的对（安慰剂，n=200）
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
from sqlalchemy import create_engine
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
TRAIN = ("2021-01-01", "2023-12-31")
K_GRID = [1.5, 2.0, 3.0, 5.0, 8.0, None]   # None = 不过滤
MAX_MDD_TRAIN = 0.25
N_PLACEBO = 200
SEED = 20261102


def leg_risk_pct(t: dict, dim: dict, e0: float) -> float:
    """1 手风险占当前权益的比例（%）。mult 来自 dim。"""
    code = t["symbol"][:-3] if t["symbol"].endswith("888") else t["symbol"]
    mult = dim[code.upper()][0]
    return SL_MULT * t["risk"] * mult / e0 * 100.0


def group_pairs(trades: list[dict]) -> dict:
    g = defaultdict(list)
    for t in trades:
        g[(str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL))].append(t)
    return g


def apply_k(trades: list[dict], dim: dict, k: float | None, e0: float, frac: float) -> tuple:
    """按对过滤：任一条腿 1 手风险 > k×目标 → 整对剔除。返回 (kept, skipped_groups)。"""
    if k is None:
        return trades, 0
    target_pct = frac * 100.0
    g = group_pairs(trades)
    drop = set()
    for key, legs in g.items():
        if any(leg_risk_pct(t, dim, e0) > k * target_pct for t in legs):
            drop.add(key)
    kept = [t for t in trades
            if (str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL)) not in drop]
    return kept, len(drop)


def run(trades, closes, calendar, dim, frac, e0, *, k=None):
    kept, n_skip = apply_k(trades, dim, k, e0, frac)
    nav, cal, events, diag, _lots = simulate_fx(kept, closes, calendar, dim, frac, "risk",
                                                e0=e0, mtm=True)
    ret = np.asarray(nav, dtype=float) / e0
    peak = np.maximum.accumulate(ret)
    mdd = float((1.0 - ret / peak).max()) if len(ret) else 0.0
    return {"n_trades": len(kept), "n_skip_pairs": n_skip,
            "nav": round(float(ret[-1]), 6), "mdd": round(mdd, 6),
            "n_breach": diag.get("n_margin_breach", 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/aeq/b10_pairsize.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in get_settings().main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

    print("[init] 取数与建仓 ...", flush=True)
    opens, closes, highs, lows = load_market(session, symbols, start - timedelta(days=400), end,
                                             calendar_mode="weekend_drop")
    calendar = [d for d in opens.index
                if start <= (d if isinstance(d, date) else d.date()) <= end]
    atr = daily_atr(highs, lows, closes)
    cmap = main_contract_surface(session, symbols, list(opens.index))
    tgt, _ = compute_targets(closes, pairs=PAIRS_ALL, window=60, entry_z=2.0, exit_z=0.5,
                             return_strength=True)
    t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
    trades = simulate_windows(t, opens, closes, atr, wfp, start, end, rebalance_n=1,
                              sl_mult=SL_MULT, contract_map=cmap, coef_cache={})
    print(f"[init] n_trades={len(trades)} 对={len(group_pairs(trades))}", flush=True)

    out = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "e0": E0, "frac": FRAC, "k_grid": K_GRID,
                    "train": TRAIN, "max_mdd_train": MAX_MDD_TRAIN,
                    "rule": "按 (entry_dt, pair) 分组，任一腿 1 手风险 > k×目标 → 整对跳过",
                    "prereg": "k* = 训练期 argmax NAV s.t. MDD<=0.25；并列取较小 k"},
           "n_trades": len(trades), "n_pairs": len(group_pairs(trades))}

    def slice_trades(a: str, b: str):
        return [x for x in trades if a <= str(x["entry_dt"])[:10] <= b]

    # ---------- 训练期网格 ----------
    print("\n[T] 训练期网格 2021-2023", flush=True)
    tr = slice_trades(*TRAIN)
    tr_cal = [d for d in calendar if TRAIN[0] <= str(d)[:10] <= TRAIN[1]]
    grid = {}
    for k in K_GRID:
        r = run(tr, closes, tr_cal, dim, FRAC, E0, k=k)
        grid["inf" if k is None else str(k)] = r
        print(f"   k={str(k):>5}: trades={r['n_trades']:>3} skip_pairs={r['n_skip_pairs']:>3} "
              f"nav={r['nav']:.4f} mdd={r['mdd']:.4f}", flush=True)

    ok = [(kk, v) for kk, v in grid.items() if v["mdd"] <= MAX_MDD_TRAIN]
    if ok:
        kk_best = max(ok, key=lambda kv: (kv[1]["nav"], -float(kv[0]) if kv[0] != "inf" else -1e9))[0]
    else:
        kk_best = "inf"
    k_star = None if kk_best == "inf" else float(kk_best)
    print(f"   → 训练期选定 k*={kk_best} (MDD约束{'满足' if ok else '全不满足，退化为不过滤'})",
          flush=True)
    out["T_train_grid"] = grid
    out["k_star"] = kk_best

    # ---------- OOS ----------
    print("\n[O] OOS 2024-2026", flush=True)
    oo = [x for x in trades if str(x["entry_dt"])[:10] >= "2024-01-01"]
    oo_cal = [d for d in calendar if str(d)[:10] >= "2024-01-01"]
    base = run(oo, closes, oo_cal, dim, FRAC, E0, k=None)
    filt = run(oo, closes, oo_cal, dim, FRAC, E0, k=k_star)
    print(f"   基线: trades={base['n_trades']} nav={base['nav']:.4f} mdd={base['mdd']:.4f}")
    print(f"   k*={kk_best}: trades={filt['n_trades']} skip_pairs={filt['n_skip_pairs']} "
          f"nav={filt['nav']:.4f} mdd={filt['mdd']:.4f}")
    out["O_oos"] = {"baseline": base, "filtered": filt,
                    "delta_nav": round(filt["nav"] - base["nav"], 6),
                    "delta_mdd": round(filt["mdd"] - base["mdd"], 6)}

    # ---------- 逐年 ----------
    years = {}
    for y in ("2024", "2025", "2026"):
        sel = [x for x in trades if str(x["entry_dt"])[:4] == y]
        cal_y = [d for d in calendar if str(d)[:4] == y]
        b = run(sel, closes, cal_y, dim, FRAC, E0, k=None)
        f = run(sel, closes, cal_y, dim, FRAC, E0, k=k_star)
        years[y] = {"baseline": b, "filtered": f}
        print(f"   {y}: 基线 nav={b['nav']:.4f} mdd={b['mdd']:.4f} | "
              f"过滤 nav={f['nav']:.4f} mdd={f['mdd']:.4f} (跳过 {f['n_skip_pairs']} 对)")
    out["Y_by_year"] = years

    # ---------- C1 高 E0 对照 ----------
    print("\n[C1] 高 E0=1e8（1 手地板应消失）", flush=True)
    c1 = {"e0": 1e8, "baseline": run(oo, closes, oo_cal, dim, FRAC, 1e8, k=None),
          "filtered": run(oo, closes, oo_cal, dim, FRAC, 1e8, k=k_star)}
    print(f"   基线 nav={c1['baseline']['nav']:.4f} mdd={c1['baseline']['mdd']:.4f}")
    print(f"   过滤 nav={c1['filtered']['nav']:.4f} mdd={c1['filtered']['mdd']:.4f} "
          f"skip={c1['filtered']['n_skip_pairs']}")
    out["C1_high_e0"] = c1

    # ---------- C2 安慰剂：随机跳过同数量对 ----------
    print(f"\n[C2] 随机跳过同数量对（n={N_PLACEBO}）", flush=True)
    g = group_pairs(oo)
    keys = list(g)
    n_skip = filt["n_skip_pairs"]
    rng = np.random.default_rng(SEED)
    navs = []
    for _ in range(N_PLACEBO):
        drop = set(rng.choice(len(keys), size=min(n_skip, len(keys)), replace=False))
        dk = {keys[i] for i in drop}
        kept = [x for x in trades if str(x["entry_dt"])[:10] >= "2024-01-01"
                and (str(x["entry_dt"])[:10], pair_of(x["symbol"], PAIRS_ALL)) not in dk]
        nav, _cal, _ev, _dg, _lt = simulate_fx(kept, closes, oo_cal, dim, FRAC, "risk",
                                               e0=E0, mtm=True)
        navs.append(float(np.asarray(nav)[-1] / E0))
    navs = np.asarray(navs)
    pct = float((navs >= filt["nav"]).mean())
    print(f"   随机跳过 {n_skip} 对: nav 中位={np.median(navs):.4f} "
          f"p95={np.percentile(navs,95):.4f} | 过滤后={filt['nav']:.4f} 分位={pct:.1%}")
    out["C2_placebo"] = {"n": N_PLACEBO, "median": round(float(np.median(navs)), 6),
                         "p95": round(float(np.percentile(navs, 95)), 6),
                         "max": round(float(navs.max()), 6),
                         "share_ge_filtered": round(pct, 4)}

    v = []
    if filt["nav"] > base["nav"] and filt["mdd"] < base["mdd"]:
        v.append("OOS 同时改善收益与回撤")
    elif filt["nav"] > base["nav"] or filt["mdd"] < base["mdd"]:
        v.append("OOS 单向改善")
    else:
        v.append("OOS 未改善")
    if pct < 0.10:
        v.append("安慰剂分位<10%，改善非随机跳过所致")
    if c1["filtered"]["n_skip_pairs"] == 0:
        v.append("E0=1e8 时无需跳过 → 支持'规模错配'解释")
    out["verdict"] = "；".join(v)
    print(f"\n[verdict] {out['verdict']}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"[done] → {args.out}")


if __name__ == "__main__":
    main()
