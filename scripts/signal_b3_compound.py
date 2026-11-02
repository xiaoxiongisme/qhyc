#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六期 P2：B3 真实动态复利（按权益缩放 + 整数手数 + 逐日盯市），§21 上界的现实化。

§21 的「固定分数复利」(NAV 7.72/47.39/804.89) 隐含「无并行冲突、可无限细分头寸」，
被明确标注为上界、不可当可提取收益。本脚本构建更贴近生产的口径：

  * 按**当前权益**动态决定每笔新开仓手数：budget = E_mark × frac；
    size_basis='notional' -> lots = floor(budget / (entry_px × multiplier))
    size_basis='risk'     -> lots = floor(budget / (sl_mult × ATR × multiplier))
  * **整数手数**：lots<1 时强制 1 手并计数（保持成交集不变，便于与 §21 逐位比较；
    但会标注「最小 1 手使小额账户实际风险高于目标 frac」）
  * **逐日盯市**：按日线 close 估浮动盈亏，E_mark = E0 + 已实现 + 浮动；
    已实现盈亏仍用 trade 的 open-to-open pnl 扣成本（与 §19/§21 逐位一致）
  * **并行约束**：统计保证金占用率与超限次数（主口径不跳过开仓以保全成交集，
    另给 enforce_margin 档跳过超限开仓，体现真实容量约束）
  * 初始资金 E0=1e6（与 §21 等名义本金 1,899,328.69 ≈ 1e6×1.89933 对齐）

日历口径默认 weekend_drop（P1 主修正口径），另跑 raw 作对照。
诚实边界：不含资金成本/利息、组合保证金优惠、滑点随规模变化、逐日结算价
（用 close 近似 settle）、法定节假日；整数手数下 frac 敏感性受最小 1 手截断。

用法：python signal_b3_compound.py --out /tmp/aeq/b3_compound.json
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
                                 simulate_windows, seg_stats, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS
from signal_b3_equity import _d

PAIRS_ALL = list(BASE_PAIRS) + EXT_PAIRS
E0 = 1_000_000.0
FRACS = (0.01, 0.02, 0.05, 0.10)
BASES = ("notional", "risk")
MODES = ("weekend_drop", "raw")


def load_dim(session):
    rows = session.execute(text("""
        SELECT variety_code, multiplier, margin_rate_long, margin_rate_short
        FROM l3_ref.dim_variety
    """)).all()
    return {str(v).upper(): (float(m or 0), float(rl or 0), float(rs or 0))
            for v, m, rl, rs in rows}


def compound(trades, closes, calendar, dim, frac, basis,
             enforce_margin=False, max_margin_pct=0.5):
    """按时间顺序逐日模拟；返回 NAV 曲线与摩擦统计。"""
    by_entry, by_exit = defaultdict(list), defaultdict(list)
    for i, t in enumerate(trades):
        by_entry[_d(t["entry_dt"])].append(i)
        by_exit[_d(t["exit_dt"])].append(i)

    cal = [str(d)[:10] for d in calendar]
    close_s = closes.copy()
    close_s.index = [str(x)[:10] for x in close_s.index]

    def close_at(ds, sym):
        try:
            v = float(close_s.loc[ds, sym])
            return None if v != v else v
        except Exception:
            return None

    realized = 0.0
    open_pos = {}          # i -> {lots, mult, sign, margin}
    nav, nav_abs_dd = [], []
    n_forced_one, n_margin_breach, n_skipped_margin, n_missing_margin = 0, 0, 0, 0
    lot_list = []
    peak = E0
    for ds in cal:
        # 1) 结算当日平仓（open-to-open，与 §19/§21 逐位一致）
        for i in by_exit[ds]:
            p = open_pos.pop(i, None)
            if p is None:
                continue
            t = trades[i]
            realized += (t["pnl"] - t["cost_pts"]) * p["mult"] * p["lots"]
        # 2) 逐日盯市浮动 + 保证金占用
        floating, margin_used = 0.0, 0.0
        for i, p in open_pos.items():
            t = trades[i]
            px = close_at(ds, t["symbol"])
            if px is not None:
                floating += p["sign"] * (px - t["entry_px"]) * p["mult"] * p["lots"]
            margin_used += p["margin"]
        e_mark = E0 + realized + floating
        # 3) 当日开仓
        for i in by_entry[ds]:
            t = trades[i]
            var = t["symbol"][:-3] if t["symbol"].endswith("888") else t["symbol"]
            d = dim.get(var.upper())
            if not d or d[0] <= 0:
                n_missing_margin += 1
                continue
            mult, rl, rs = d
            rate = rl if t["side"] == "LONG" else rs
            if rate <= 0:
                n_missing_margin += 1
                continue
            margin_pl = t["entry_px"] * mult * rate
            denom = (t["entry_px"] * mult) if basis == "notional" \
                else (SL_MULT * t["risk"] * mult)
            budget = e_mark * frac
            lots = int(budget // denom) if denom > 0 else 0
            if lots < 1:
                n_forced_one += 1
                lots = 1
            if margin_used + lots * margin_pl > e_mark * max_margin_pct:
                n_margin_breach += 1
                if enforce_margin:
                    n_skipped_margin += 1
                    continue
            sign = 1.0 if t["side"] == "LONG" else -1.0
            open_pos[i] = {"lots": lots, "mult": mult, "sign": sign,
                           "margin": lots * margin_pl}
            margin_used += lots * margin_pl
            lot_list.append(lots)

        peak = max(peak, e_mark)
        nav.append(e_mark)
        nav_abs_dd.append((peak - e_mark) / E0)

    arr = np.array(nav, dtype=float)
    ret = np.diff(arr) / arr[:-1] if len(arr) > 1 else np.array([])
    sd = float(ret.std(ddof=1)) if len(ret) > 1 else 0.0
    sharpe = float(ret.mean() / sd * np.sqrt(252)) if sd > 0 else None
    span_days = (date.fromisoformat(cal[-1]) - date.fromisoformat(cal[0])).days or 1
    years = span_days / 365.25
    total = float(arr[-1] / E0 - 1)
    cagr = float((arr[-1] / E0) ** (1 / years) - 1) if years > 0 and arr[-1] > 0 else None
    return {
        "frac": frac, "size_basis": basis, "enforce_margin": enforce_margin,
        "max_margin_pct": max_margin_pct,
        "final_nav": round(float(arr[-1]), 2),
        "nav_multiple": round(float(arr[-1] / E0), 4),
        "total_return": round(total, 5),
        "cagr": round(cagr, 5) if cagr is not None else None,
        "sharpe_daily": round(sharpe, 3) if sharpe is not None else None,
        "mdd_over_E0": round(float(max(nav_abs_dd)), 5),
        "n_forced_one_lot": n_forced_one,
        "n_margin_breach": n_margin_breach,
        "n_skipped_by_margin": n_skipped_margin,
        "n_missing_margin_data": n_missing_margin,
        "mean_lots": round(float(np.mean(lot_list)), 2) if lot_list else None,
        "max_lots": int(max(lot_list)) if lot_list else None,
        "curve_head": [round(x, 2) for x in arr[:5]],
        "curve_tail": [round(x, 2) for x in arr[-5:]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--max-margin-pct", type=float, default=0.5)
    ap.add_argument("--out", default="/tmp/aeq/b3_compound.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)
    print(f"[init] E0={E0:.0f} n_dim_varieties={len(dim)} fracs={FRACS} bases={BASES}",
          flush=True)

    report = {"meta": {
        "start": str(start), "end": str(end), "E0": E0,
        "fracs": list(FRACS), "size_bases": list(BASES),
        "calendar_modes": list(MODES), "max_margin_pct": args.max_margin_pct,
        "purpose": "六期 P2 真实动态复利（权益缩放+整数手数+逐日盯市）",
        "caveats": [
            "已实现盈亏用 trade 的 open-to-open pnl 扣成本，与 §19/§21 逐位一致；逐日盯市用日线 close 近似结算价，仅供权益/保证金判定",
            "lots<1 强制 1 手以保持成交集不变（便于与 §21 比较），会使小额账户实际风险高于目标 frac，已计数",
            "主口径不跳过保证金超限开仓（保全成交集），另给 enforce_margin 档体现真实容量约束",
            "不含资金成本/利息、组合保证金优惠、滑点随规模变化、法定节假日；session_map 未纳入（其 open 语义改变见 §26 P1）",
            "复利结果对初始资金 E0、frac、整数手数截断敏感，属情景分析而非收益承诺",
        ],
    }, "modes": {}}

    for mode in MODES:
        print(f"\n{'='*20} calendar_mode={mode} {'='*20}", flush=True)
        opens, closes, highs, lows = load_market(
            session, symbols, start - timedelta(days=400), end, calendar_mode=mode)
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
        simple = sum((x["pnl"] - x["cost_pts"]) / x["entry_px"] for x in trades)
        print(f"  n_trades={len(trades)} 简单加总归一收益={simple:.5f} 日历={len(calendar)}",
              flush=True)

        cells = []
        for basis in BASES:
            for frac in FRACS:
                r = compound(trades, closes, calendar, dim, frac, basis,
                             enforce_margin=False,
                             max_margin_pct=args.max_margin_pct)
                cells.append(r)
                print(f"  {basis:9s} frac={frac:.2f} NAV={r['nav_multiple']:>8.3f}x "
                      f"CAGR={r['cagr']} MDD/E0={r['mdd_over_E0']} "
                      f"强制1手={r['n_forced_one_lot']} 超保证金={r['n_margin_breach']} "
                      f"均手数={r['mean_lots']}", flush=True)
            # 现实约束档：跳过保证金超限开仓
            r2 = compound(trades, closes, calendar, dim, 0.05, basis,
                          enforce_margin=True,
                          max_margin_pct=args.max_margin_pct)
            r2["note"] = "enforce_margin=True：跳过保证金超限的新开仓（成交集改变）"
            cells.append(r2)
            print(f"  {basis:9s} frac=0.05 [enforce_margin] NAV={r2['nav_multiple']:.3f}x "
                  f"跳过={r2['n_skipped_by_margin']} MDD/E0={r2['mdd_over_E0']}", flush=True)

        report["modes"][mode] = {
            "n_trades": len(trades),
            "n_calendar_days": len(calendar),
            "simple_sum_return_px": round(simple, 5),
            "cells": cells,
        }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
