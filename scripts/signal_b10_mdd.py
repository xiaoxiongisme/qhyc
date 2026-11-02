"""十期④ 风控指标统一（MDD 单一定义）+ 门槛重算

问题（§30.3 R5）：项目里同时流通三套 MDD，跨期混用：
  A 已实现累计收益点（signal_b3_equity.equity_curve，2026=0.10284）
  B 盯市 ÷ E0（simulate_fx/slice_stats，2026=0.3534）
  C 盯市 ÷ 运行峰值（bootstrap/frac_power）
§30 定的纪律是"对外只报盯市"，但没指定分母。本脚本把口径钉死并全期重算：

  主口径 MDD_peak = max(1 - NAV_t / cummax(NAV_t))   —— 盯市、以运行峰值为分母
  辅口径 MDD_e0   = max(1 - NAV_t / E0)              —— 初始化资金为分母（更严厉）
  两者都报，门禁用 MDD_peak。

同时把第 3 道门槛（原"2024/25/26 不崩"= 收益符号）改为
  **MDD_peak < 25% 且 年收益 > -10%**
理由是收益符号对配仓规模敏感（§30 R4：2026 Σnet_R>0 但离散复利为负），
而 MDD 才是风险管理关心的量。

对照组：基线 B3 与 十期③ 的 k*=5 按对过滤版。
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
K_STAR = 5.0
YEARS = ["2021", "2022", "2023", "2024", "2025", "2026"]
MDD_GATE = 0.25
RET_GATE = -0.10


def metrics(trades, closes, calendar, dim, frac, e0, k=None):
    """统一口径指标。"""
    sel = trades
    n_skip = 0
    if k is not None:
        target = frac * 100.0
        g = defaultdict(list)
        for t in trades:
            g[(str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL))].append(t)
        drop = {key for key, legs in g.items()
                if any(SL_MULT * t["risk"] * dim[(t["symbol"][:-3] if t["symbol"].endswith("888")
                                                  else t["symbol"]).upper()][0] / e0 * 100.0
                       > k * target for t in legs)}
        sel = [t for t in trades
               if (str(t["entry_dt"])[:10], pair_of(t["symbol"], PAIRS_ALL)) not in drop]
        n_skip = len(drop)
    nav, _c, _e, diag, _l = simulate_fx(sel, closes, calendar, dim, frac, "risk", e0=e0, mtm=True)
    ret = np.asarray(nav, dtype=float) / e0
    peak = np.maximum.accumulate(ret)
    mdd_peak = float((1.0 - ret / peak).max()) if len(ret) else 0.0
    # MDD_e0：peak 初值取 E0（即 ret=1.0），故等价于 1 - min(ret)
    mdd_e0 = float(max(0.0, 1.0 - float(ret.min()))) if len(ret) else 0.0
    net = np.array([t["net"] for t in sel], dtype=float)
    gains = net[net > 0].sum()
    losses = -net[net < 0].sum()
    return {"n_trades": len(sel), "n_skip_pairs": n_skip,
            "nav": round(float(ret[-1]), 6),
            "ret": round(float(ret[-1] - 1.0), 6),
            "mdd_peak": round(mdd_peak, 6), "mdd_e0": round(mdd_e0, 6),
            "net_R": round(float(net.sum()), 5),
            "pf": round(float(gains / losses), 4) if losses > 0 else None,
            "win": round(float((net > 0).mean()), 4) if len(net) else None,
            "n_breach": diag.get("n_margin_breach", 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/aeq/b10_mdd.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in get_settings().main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

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
    print(f"[init] n_trades={len(trades)}", flush=True)

    out = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "e0": E0, "frac": FRAC, "k_star": K_STAR,
                    "primary_mdd": "MDD_peak = max(1 - NAV/peak)", "secondary": "MDD_e0 = 1 - min(NAV/E0)",
                    "gate3": f"MDD_peak < {MDD_GATE} 且 年收益 > {RET_GATE}"}}

    print("\n[全期]")
    full_b = metrics(trades, closes, calendar, dim, FRAC, E0, k=None)
    full_f = metrics(trades, closes, calendar, dim, FRAC, E0, k=K_STAR)
    for nm, m in (("基线", full_b), ("k*=5", full_f)):
        print(f"   {nm}: trades={m['n_trades']} nav={m['nav']:.4f} ret={m['ret']:+.2%} "
              f"mdd_peak={m['mdd_peak']:.4f} mdd_e0={m['mdd_e0']:.4f} netR={m['net_R']:.3f} "
              f"pf={m['pf']}")
    out["full"] = {"baseline": full_b, "filtered_k5": full_f}

    print("\n[逐年] 统一 MDD 口径")
    by_year = {}
    for y in YEARS:
        sel = [x for x in trades if str(x["entry_dt"])[:4] == y]
        cal_y = [d for d in calendar if str(d)[:4] == y]
        if not sel or not cal_y:
            continue
        b = metrics(sel, closes, cal_y, dim, FRAC, E0, k=None)
        f = metrics(sel, closes, cal_y, dim, FRAC, E0, k=K_STAR)
        b["gate3_pass"] = (b["mdd_peak"] < MDD_GATE and b["ret"] > RET_GATE)
        f["gate3_pass"] = (f["mdd_peak"] < MDD_GATE and f["ret"] > RET_GATE)
        by_year[y] = {"baseline": b, "filtered_k5": f}
        print(f"   {y}: 基线 ret={b['ret']:+.2%} mdd={b['mdd_peak']:.4f} "
              f"mdd_e0={b['mdd_e0']:.4f} 门槛3={'过' if b['gate3_pass'] else '不过'} | "
              f"k5 ret={f['ret']:+.2%} mdd={f['mdd_peak']:.4f} "
              f"门槛3={'过' if f['gate3_pass'] else '不过'}")
    out["by_year"] = by_year

    # 旧口径 vs 新口径：门槛 3 判定翻转清单
    flips = []
    for y, v in by_year.items():
        for tag in ("baseline", "filtered_k5"):
            m = v[tag]
            old = m["ret"] > 0
            new = m["gate3_pass"]
            if old != new:
                flips.append({"year": y, "variant": tag, "old_gate3": old, "new_gate3": new,
                              "ret": m["ret"], "mdd_peak": m["mdd_peak"]})
    out["gate3_flips"] = flips
    print(f"\n[门槛3 判定翻转] {len(flips)} 处: " +
          ", ".join(f"{x['year']}/{x['variant']}(旧{'过' if x['old_gate3'] else '不过'}→新{'过' if x['new_gate3'] else '不过'})"
                    for x in flips))

    # 五门槛汇总
    gates = {}
    for tag, m in (("baseline", full_b), ("filtered_k5", full_f)):
        yrs = {y: by_year[y][tag] for y in by_year}
        gates[tag] = {
            "G1_net_R_pos": m["net_R"] > 0,
            "G2_pf_ge_1.1": (m["pf"] or 0) >= 1.1,
            "G3_years_ok": all(v["gate3_pass"] for v in yrs.values()),
            "G3_failing_years": [y for y, v in yrs.items() if not v["gate3_pass"]],
            "G4_placebo": "复用 §27 结果：三口径安慰剂 0.0%（本轮未重跑）",
            "G5_corr_v2": "复用 §27：Pearson −0.0158/+0.0037/+0.0031（|ρ|≪0.30）",
        }
    out["five_gates"] = gates
    for tag, g in gates.items():
        print(f"   {tag}: G1={g['G1_net_R_pos']} G2={g['G2_pf_ge_1.1']} "
              f"G3={g['G3_years_ok']} 未过年份={g['G3_failing_years']}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"[done] → {args.out}")


if __name__ == "__main__":
    main()
