#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四期 P1：B3 生产评估补「归一收益率 / 资金曲线」（§11.8 待办①）。

§11.6 发现：R 口径按 sl_mult×日线 ATR 归一会**放大/反转薄毛利对**
（RB-HC sum_net_R +0.41 但归一收益率 −0.025；FG-SA 成本/毛利 330%）。
故生产评估须以归一收益率 + 资金曲线为主判据，R 仅作风险归一参考。

本脚本在底座原始成交上**事后重算**（不改 simulate，与 §9/§10/§11 逐位可比）：
  * 归一收益率：`(pnl − cost×mult) / entry_px`，跨品种可比（无量纲，×100 即 % 名义本金）。
  * 资金曲线：按**完整交易日历**（无成交日填 0）逐日累计，固定名义本金、简单加总、不计复利
    （B3 为多对并行持仓，等名义本金假设已在 §11 说明；复利口径不作主张）。
  * 指标：总收益、年化(CAGR)、日度 Sharpe(√252)、MDD、Calmar、分年收益/MDD/笔数、
    暴露天数、换手笔数；并给 R 口径对照，量化两种口径差异。
  * 逐对：归一收益率 vs sum_net_R 符号对照，坐实 §11.6 的扭曲。

基线 ext:60:2.0 须与 §10（n=195 / 净R 0.19007 / PF 1.734）逐位一致。

用法：python signal_b3_equity.py --pair-set ext --window 60 --entry-z 2.0 --out ...
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
from signal_b3_capacity import r_stats, amount_stats, pair_of


def _d(t):
    """统一成 'YYYY-MM-DD'，避免 _dt() 返回 datetime 时 str() 带时间部分导致日历 key 不匹配。"""
    return str(_dt(t))[:10]


def equity_curve(trades, calendar, denom="return", cost_mult=1.0):
    """按完整交易日历构造资金曲线（无成交日填 0）。

    denom='return' -> (pnl−cost)/entry_px ；denom='R' -> (pnl−cost)/risk
    返回日度收益、累计曲线、指标；固定名义本金、简单加总、不计复利。
    """
    if not trades:
        return {"n": 0}
    day = defaultdict(float)
    for t in trades:
        num = t["pnl"] - t["cost_pts"] * cost_mult
        den = t["entry_px"] if denom == "return" else t["risk"]
        if den == 0 or den != den:
            continue
        day[_d(t["entry_dt"])] += num / den
    cal = [str(d)[:10] for d in calendar]
    r = np.array([day.get(d, 0.0) for d in cal], dtype=float)
    eq = np.cumsum(r)
    total = float(eq[-1])
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    mdd_v = float(dd.max())
    active = int((r != 0).sum())
    sd = float(r.std(ddof=1))
    sharpe = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else None
    # 年化用日历跨度（首尾交易日）
    span_days = (date.fromisoformat(cal[-1]) - date.fromisoformat(cal[0])).days or 1
    years = span_days / 365.25
    cagr = (float((1 + total) ** (1 / years)) - 1) if (years > 0 and total > -1) else None
    wins, losses = r[r > 0], r[r < 0]
    return {
        "n_trades": len(trades), "n_calendar_days": len(cal), "n_active_days": active,
        "exposure_pct": round(100 * active / len(cal), 2),
        "total_return": round(total, 5),
        "cagr": round(cagr, 5) if cagr is not None else None,
        "sharpe_daily": round(sharpe, 3) if sharpe is not None else None,
        "mdd": round(mdd_v, 5),
        "calmar": round(total / mdd_v, 3) if mdd_v > 0 else None,
        "win_day_pct": round(100 * len(wins) / max(1, len(wins) + len(losses)), 2),
        "span_years": round(years, 2),
        "curve_head": [round(x, 5) for x in eq[:5]],
        "curve_tail": [round(x, 5) for x in eq[-5:]],
    }


def by_year_equity(trades, calendar, denom="return"):
    """分年：每年用自己的交易日子集算收益、MDD、笔数。"""
    groups = defaultdict(list)
    for t in trades:
        groups[_d(t["entry_dt"])[:4]].append(t)
    out = {}
    for y in sorted(groups):
        cal = [d for d in calendar if str(d)[:4] == y]
        ec = equity_curve(groups[y], cal, denom=denom)
        rs = r_stats(groups[y])
        out[y] = {"n": rs["n"], "mean_net_R": rs.get("mean_net_R"),
                  "PF": rs.get("profit_factor"),
                  "total_return": ec.get("total_return"), "mdd": ec.get("mdd"),
                  "sharpe_daily": ec.get("sharpe_daily"),
                  "exposure_pct": ec.get("exposure_pct")}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--pair-set", choices=["base", "ext"], default="ext")
    ap.add_argument("--out", default="/tmp/aeq/b3_equity.json")
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
    session.close(); engine.dispose()
    calendar = [d for d in opens.index if start <= (d if isinstance(d, date) else d.date()) <= end]
    print(f"[load] n_dates={len(opens.index)} calendar_in_window={len(calendar)}", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    tgt, _ = compute_targets(closes, pairs=pairs, window=args.window,
                             entry_z=args.entry_z, exit_z=args.exit_z,
                             return_strength=True)
    t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
    trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                              rebalance_n=1, sl_mult=SL_MULT,
                              contract_map=contract_map, coef_cache=coef_cache)
    base = seg_stats(trades)
    print(f"[baseline] n={base.get('n')} 净R={base.get('mean_net_R')} "
          f"PF={base.get('profit_factor')} (须与 §10 ext:60:2.0 n=195/0.19007/1.734 一致)",
          flush=True)

    ec_ret = equity_curve(trades, calendar, denom="return")
    ec_r = equity_curve(trades, calendar, denom="R")
    print("\n=== 资金曲线主判据（归一收益率，固定名义本金、简单加总、无复利）===", flush=True)
    print(f"  总收益={ec_ret['total_return']} 年化CAGR={ec_ret['cagr']} "
          f"Sharpe(日)={ec_ret['sharpe_daily']} MDD={ec_ret['mdd']} Calmar={ec_ret['calmar']} "
          f"暴露={ec_ret['exposure_pct']}% 成交{ec_ret['n_trades']}笔", flush=True)
    print(f"  [R口径对照] 总R={ec_r['total_return']} MDD_R={ec_r['mdd']} "
          f"Sharpe_R={ec_r['sharpe_daily']}", flush=True)

    by_year = by_year_equity(trades, calendar, denom="return")
    print("\n=== 分年（归一收益率为主）===", flush=True)
    print("  year   n   PF     总收益     MDD     Sharpe   暴露%", flush=True)
    for y, v in by_year.items():
        print(f"  {y}  {v['n']:3d}  {str(v['PF']):6s}  {str(v['total_return']):9s}  "
              f"{str(v['mdd']):7s}  {str(v['sharpe_daily']):7s}  {v['exposure_pct']}", flush=True)

    # 逐对：归一收益率 vs sum_net_R 符号对照（坐实 §11.6）
    bp = defaultdict(list)
    for tr in trades:
        bp[pair_of(tr["symbol"], pairs)].append(tr)
    per_pair = []
    print("\n=== 逐对：归一收益率 vs sum_net_R 符号对照 ===", flush=True)
    print("  pair        n   净R     sum_net_R  归一收益率  符号一致?", flush=True)
    for p, tr in sorted(bp.items()):
        rs = r_stats(tr)
        am = amount_stats(tr)
        sr, nr = rs.get("sum_net_R"), am.get("sum_net_return")
        same = (sr is not None and nr is not None and
                ((sr > 0) == (nr > 0)))
        per_pair.append({"pair": p, "n": rs["n"], "mean_net_R": rs.get("mean_net_R"),
                         "sum_net_R": sr, "sum_net_return": nr, "PF": rs.get("profit_factor"),
                         "sign_consistent": same})
        print(f"  {p:10s} {rs['n']:3d}  {str(rs.get('mean_net_R')):7s}  "
              f"{str(sr):10s}  {str(nr):11s}  {same}", flush=True)
    n_flip = sum(1 for x in per_pair if not x["sign_consistent"])
    print(f"  符号不一致对数 = {n_flip}/{len(per_pair)}", flush=True)

    # 成本压力下的资金曲线（×2/×3，沿用 §11 口径）
    stress = {}
    for m in (1.0, 2.0, 3.0):
        stress[str(m)] = equity_curve(trades, calendar, denom="return", cost_mult=m)
    print("\n=== 成本压力下的资金曲线（归一收益率）===", flush=True)
    for m, v in stress.items():
        print(f"  mult={m}: 总收益={v['total_return']} Sharpe={v['sharpe_daily']} "
              f"MDD={v['mdd']} Calmar={v['calmar']}", flush=True)

    report = {
        "meta": {"start": str(start), "end": str(end), "pair_set": args.pair_set,
                 "n_pairs": len(pairs), "window": args.window, "entry_z": args.entry_z,
                 "exit_z": args.exit_z,
                 "amount_rule": "归一收益率 (pnl−cost)/entry_px；固定名义本金、简单加总、无复利",
                 "calendar_rule": "完整交易日历，无成交日填 0；Sharpe 用日度序列年化 √252",
                 "caveat": "多对并行持仓的等名义本金假设见 §11；CAGR 由日历跨度与总收益推算，"
                           "简单加总口径下仅供参考，不等同真实复利资金曲线",
                 "n_windows": len(list(_windows(start, end, wfp)))},
        "baseline": base,
        "equity_return": ec_ret, "equity_R": ec_r,
        "by_year": by_year, "per_pair": per_pair, "n_sign_inconsistent_pairs": n_flip,
        "cost_stress_equity": stress,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
