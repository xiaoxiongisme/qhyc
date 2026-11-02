#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九期 P3：数据边界复查 + 真正新增完整交易日的 MDD 滚动跟踪（§29.5）

六期/八期均记录「结构性时间缺口」：end 固定 2026-10-30，之后 bar 陆续落盘但不进回测。
本脚本先按**预注册的完整性判据**判定 2026-11-02 是否已是「完整交易日」，再推进 end 实证：

  完整性判据（运行前定死）：
    D1 日盘完整：存在 bucket=15:00 的 bar，且覆盖品种数 ≥ 45（50 个主连中允许缺 5）
    D2 夜盘完整：session 归属 11-02 的 22:00/23:00 bar 存在（夜间 21:00 开盘后才有）
    D3 结算价可用：public.daily_bar 存在 trade_date=11-02 的 888 行
  三条全满足才判定「完整交易日」，只满足 D1 记「日盘完整、夜盘未到」。

用法：python signal_b3_boundary_s9.py --out /tmp/aeq/b3_boundary_s9.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, SL_MULT)
from signal_b3_pairs import compute_targets
from signal_b3_equity import _d
from signal_b3_compound import E0
from signal_b3_compound_oos import PAIRS_ALL
from signal_b3_dd2026_s6 import dd_attribution

MIN_SYMBOLS_DAYCLOSE = 45
PROBE_DAY = "2026-11-02"
BASE_END = "2026-10-30"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--calendar-mode", default="weekend_drop")
    ap.add_argument("--out", default="/tmp/aeq/b3_boundary_s9.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()

    # ---------- 边界快照 ----------
    row = session.execute(text(
        "SELECT max(bucket)::text, count(*) FILTER (WHERE bucket::date >= :d), "
        "count(DISTINCT bucket::date) FILTER (WHERE bucket::date >= :d) "
        "FROM l1_mkt.bar_60m"
    ), {"d": BASE_END}).one()
    hours = session.execute(text(
        """SELECT EXTRACT(hour FROM bucket)::int, count(*) FROM l1_mkt.bar_60m
           WHERE bucket::date = :d GROUP BY 1 ORDER BY 1"""
    ), {"d": PROBE_DAY}).all()
    dayclose = session.execute(text(
        """SELECT count(DISTINCT symbol) FROM l1_mkt.bar_60m
           WHERE bucket::date = :d AND EXTRACT(hour FROM bucket) = 15"""
    ), {"d": PROBE_DAY}).one()[0]
    prev_dayclose = session.execute(text(
        """SELECT count(DISTINCT symbol) FROM l1_mkt.bar_60m
           WHERE bucket::date = :d AND EXTRACT(hour FROM bucket) = 15"""
    ), {"d": BASE_END}).one()[0]
    daily_max = session.execute(text(
        "SELECT max(trade_date)::text FROM public.daily_bar WHERE symbol LIKE '%888%'"
    )).scalar()
    daily_has = session.execute(text(
        "SELECT count(*) FROM public.daily_bar WHERE symbol LIKE '%888%' AND trade_date = :d"
    ), {"d": PROBE_DAY}).scalar()

    d1 = int(dayclose or 0) >= MIN_SYMBOLS_DAYCLOSE
    d2 = any(h == 22 for h, _ in hours)
    d3 = int(daily_has or 0) > 0
    boundary = {
        "max_bucket": str(row[0]),
        "n_bars_after_base_end": int(row[1] or 0),
        "n_distinct_days_after_base_end": int(row[2] or 0),
        "probe_day": PROBE_DAY,
        "probe_day_hour_counts": {int(h): int(n) for h, n in hours},
        "probe_day_symbols_with_15h_bar": int(dayclose or 0),
        "base_end_symbols_with_15h_bar": int(prev_dayclose or 0),
        "min_symbols_required": MIN_SYMBOLS_DAYCLOSE,
        "D1_dayclose_complete": bool(d1),
        "D2_night_complete": bool(d2),
        "D3_settle_available": bool(d3),
        "is_complete_trading_day": bool(d1 and d2 and d3),
        "daily_bar_max_trade_date": daily_max,
        "daily_bar_rows_probe_day": int(daily_has or 0),
        "eight_phase_max_bucket": "2026-11-02 13:00",
    }
    print(f"[boundary] max={boundary['max_bucket']} "
          f"hours={boundary['probe_day_hour_counts']} 15h_symbols={dayclose} "
          f"(base_end {prev_dayclose}) D1={d1} D2={d2} D3={d3} "
          f"→ 完整交易日={boundary['is_complete_trading_day']}", flush=True)

    # ---------- 推进 end 实证 ----------
    symbols = [s.symbol for s in settings.main_contracts]
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    runs = {}
    for tag, end_s in (("base", BASE_END), ("advanced", PROBE_DAY)):
        start = date(2021, 1, 1)
        end = date.fromisoformat(end_s)
        opens, closes, highs, lows = load_market(
            session, symbols, start - timedelta(days=400), end,
            calendar_mode=args.calendar_mode)
        calendar = [d for d in opens.index
                    if start <= (d if isinstance(d, date) else d.date()) <= end]
        atr = daily_atr(highs, lows, closes)
        cmap = main_contract_surface(session, symbols, list(opens.index))
        tgt, _ = compute_targets(closes, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=args.entry_z, exit_z=args.exit_z,
                                 return_strength=True)
        t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=cmap, coef_cache={})
        dd = dd_attribution(trades, calendar, PAIRS_ALL)
        runs[tag] = {
            "end": end_s, "n_trades": len(trades),
            "n_calendar_days": len(calendar),
            "max_entry_date": max((_d(x["entry_dt"]) for x in trades), default=None),
            "max_exit_date": max((_d(x["exit_dt"]) for x in trades), default=None),
            "dd": dd,
        }
        print(f"[{tag}] end={end_s} n_trades={len(trades)} cal={len(calendar)} "
              f"max_entry={runs[tag]['max_entry_date']} "
              f"max_exit={runs[tag]['max_exit_date']}", flush=True)
        print(f"        dd={json.dumps(dd, ensure_ascii=False)[:400]}", flush=True)

    b, a = runs["base"], runs["advanced"]
    delta = {
        "n_trades_delta": a["n_trades"] - b["n_trades"],
        "calendar_days_delta": a["n_calendar_days"] - b["n_calendar_days"],
        "baseline_reproduced": a["n_trades"] == b["n_trades"] and b["n_trades"] > 0,
        "note": ("若 n_trades_delta=0 且 calendar_days_delta>0，说明推进 end 只是加长了空日历"
                 "（结构性时间缺口仍在）；若 >0 则为真正新增成交"),
    }
    print(f"[delta] {delta}", flush=True)

    report = {"meta": {"purpose": "九期 P3：数据边界复查 + 新增完整交易日 MDD 跟踪",
                       "probe_day": PROBE_DAY, "base_end": BASE_END,
                       "calendar_mode": args.calendar_mode,
                       "criteria": {"D1": f"15:00 bar 覆盖品种数 ≥ {MIN_SYMBOLS_DAYCLOSE}",
                                    "D2": "22:00 bar 存在", "D3": "daily_bar 11-02 有 888 行"},
                       "caveats": ["bar_60m 的 bucket 为 bar 结束时刻",
                                   "l3_ref.trade_calendar 覆盖不全，不以该表判完整性"]},
              "boundary": boundary, "runs": runs, "delta": delta}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
