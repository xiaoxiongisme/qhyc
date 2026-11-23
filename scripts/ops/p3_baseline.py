# -*- coding: utf-8 -*-
"""P3 首次基线（容器内只读跑）：10 品种限价成交率/跳空/压力曲线。

不写库、不改生产运行代码；校准模块现已在 app/strategies/five_min/calibration.py，
按常规 PYTHONPATH=/app 直接运行即可（CL-20261123-01）。
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from app.data import barstore
from app.strategies.five_min.calibration import calibrate_touch, fill_rate_stress_replay
from app.strategies.five_min.indicators import attach_indicators
from app.strategies.five_min.signals import scan_signals

SYMS = ["EB888", "SR888", "FG888", "JD888", "MA888", "M888", "UR888", "PP888", "A888", "BU888"]
# 与 runner.load_contract_meta 同源：mult/tick 从生产元数据读，fee 用 dim_trading_cost
from sqlalchemy import text
from app.core.db import get_engine


def load_meta(syms):
    eng = get_engine()
    out = {}
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT symbol, multiplier, price_tick FROM public.futures_symbol "
            "WHERE symbol = ANY(:syms)"), {"syms": syms}).mappings().all()
        for r in rows:
            out[str(r["symbol"])] = {"mult": float(r["multiplier"] or 0),
                                     "tick": float(r["price_tick"] or 0), "fee": 3.0}
        for sym in list(out):
            variety = "".join(ch for ch in sym if ch.isalpha())
            fr = conn.execute(text(
                "SELECT fee_type, fee_value FROM public.dim_trading_cost "
                "WHERE variety_code=:v AND action='OPEN' AND effective_from<=CURRENT_DATE "
                "AND (effective_to IS NULL OR effective_to>=CURRENT_DATE) "
                "ORDER BY effective_from DESC, id DESC LIMIT 1"), {"v": variety}).mappings().first()
            if fr and str(fr["fee_type"]).upper() == "FIXED":
                out[sym]["fee"] = float(fr["fee_value"])
    return out


def fee_per_side(m, px):
    if "fee_pct_per_10000" in m:
        return m["fee_pct_per_10000"] / 10000.0 * px * m["mult"]
    return m.get("fee", 3.0)


def main():
    meta = load_meta(SYMS)
    rows = []
    for sym in SYMS:
        m = meta.get(sym)
        if not m or not m["mult"] or not m["tick"]:
            print(f"SKIP {sym}: no meta", file=sys.stderr)
            continue
        bars = barstore.load(sym, freq="5m", caliber="continuous", limit=112000)
        daily = barstore.load(sym, freq="daily", caliber="continuous", limit=400)
        if bars.empty:
            print(f"SKIP {sym}: no bars", file=sys.stderr)
            continue
        bars = bars.rename(columns={"ts": "dt"})
        if "ts" in daily:
            daily = daily.rename(columns={"ts": "dt"})
        ctx = attach_indicators(bars, daily)
        sigs = scan_signals(ctx)
        if not sigs:
            print(f"SKIP {sym}: no signals", file=sys.stderr)
            continue
        touch = calibrate_touch(ctx, sigs, mult=m["mult"], tick=m["tick"])
        fee = fee_per_side(m, float(ctx["close"].iloc[-1]))
        stress = fill_rate_stress_replay(ctx, sigs, sym, mult=m["mult"],
                                         tick=m["tick"], fee_per_side=fee)
        row = {"symbol": sym, "bars": len(ctx), "signals": len(sigs), **touch}
        for k, v in stress.items():
            row[k + "_n"] = v["n"]
            row[k + "_total"] = v["total"]
            row[k + "_pf"] = v["pf"]
        rows.append(row)
        print(f"{sym}: sig={len(sigs)} attempts={touch['attempts']} "
              f"touch={touch['touch_rate']} gap={touch['gap_ticks_mean']}/p90={touch['gap_ticks_p90']} "
              f"keep100_total={stress['keep_100']['total']} keep40_total={stress['keep_40']['total']}",
              file=sys.stderr)
    out = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "window": "recent ~112000 x 5m bars per symbol (matches research window)",
        "rows": rows,
        "aggregate": {
            "symbols": len(rows),
            "total_attempts": sum(r["attempts"] for r in rows),
            "total_touched": sum(r["touched"] for r in rows),
            "touch_rate": round(sum(r["touched"] for r in rows) /
                                max(1, sum(r["attempts"] for r in rows)), 4),
            "keep100_total": sum(r["keep_100_total"] for r in rows),
            "keep40_total": sum(r["keep_40_total"] for r in rows),
            "keep40_keep100_ratio": round(sum(r["keep_40_total"] for r in rows) /
                                          max(1, abs(sum(r["keep_100_total"] for r in rows))), 3),
        },
    }
    print("===JSON_BEGIN===")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    main()
