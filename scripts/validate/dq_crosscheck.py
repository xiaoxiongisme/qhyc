# -*- coding: utf-8 -*-
"""交叉验证：daily.close 到底等于 hourly 的哪一根？"""
from sqlalchemy import text
from app.core.db import session_scope

with session_scope() as s:
    for sym in ("FU888", "SC888", "CU888", "RB888"):
        print("\n" + "=" * 96)
        print(f"{sym}: daily vs hourly 明细（最近 6 个交易日）")
        print("=" * 96)
        rows = s.execute(text("""
            SELECT d.trade_date, d.close d_close, d.settle, h.last_dt, h.last_close,
                   h.n_bars, h.close_1500
            FROM (
                SELECT trade_date, close, settle FROM l0_raw.daily_bar
                WHERE symbol=:s AND src='akshare'
                ORDER BY trade_date DESC LIMIT 6
            ) d
            LEFT JOIN LATERAL (
                SELECT MAX(trade_datetime) last_dt, count(*) n_bars,
                       MAX(CASE WHEN trade_datetime::time = '15:00:00'
                                THEN close END) close_1500,
                       (ARRAY_AGG(close ORDER BY trade_datetime DESC))[1] last_close
                FROM l0_raw.hourly_bar
                WHERE symbol=:s AND trade_datetime::date = d.trade_date
            ) h ON TRUE
            ORDER BY d.trade_date DESC
        """), {"s": sym}).fetchall()
        print(f"{'日期':<12}{'daily.close':>12}{'15:00收':>12}{'最后一根':>12}"
              f"{'最后bar时间':>22}{'bar数':>6}{'15:00差':>10}")
        print("-" * 96)
        for r in rows:
            m = r._mapping
            dc = float(m["d_close"]) if m["d_close"] is not None else 0
            c15 = float(m["close_1500"]) if m["close_1500"] is not None else None
            lc = float(m["last_close"]) if m["last_close"] is not None else None
            diff = abs(dc - c15) / dc * 100 if c15 and dc else None
            print(f"{str(m['trade_date']):<12}{dc:>12.2f}"
                  f"{(f'{c15:.2f}' if c15 else 'n/a'):>12}"
                  f"{(f'{lc:.2f}' if lc else 'n/a'):>12}"
                  f"{str(m['last_dt'])[:19]:>22}{m['n_bars']:>6}"
                  f"{(f'{diff:.2f}%' if diff is not None else 'n/a'):>10}")

    print("\n" + "=" * 96)
    print("结论性统计：daily.close == hourly 15:00 close 的命中率（全库，最近120交易日）")
    print("=" * 96)
    rows = s.execute(text("""
        SELECT symbol, count(*) n,
               count(*) FILTER (WHERE c15 IS NOT NULL) has15,
               count(*) FILTER (WHERE c15 IS NOT NULL AND ABS(dc - c15) <= 0.005) exact,
               count(*) FILTER (WHERE c15 IS NOT NULL AND ABS(dc-c15) > dc*0.001) off
        FROM (
            SELECT d.symbol, d.trade_date, d.close dc, h.c15
            FROM (
                SELECT symbol, trade_date, close FROM l0_raw.daily_bar
                WHERE src='akshare' AND trade_date >= CURRENT_DATE - 180
            ) d
            LEFT JOIN LATERAL (
                SELECT MAX(CASE WHEN trade_datetime::time='15:00:00' THEN close END) c15
                FROM l0_raw.hourly_bar
                WHERE symbol=d.symbol AND trade_datetime::date=d.trade_date
            ) h ON TRUE
        ) x GROUP BY 1 ORDER BY off DESC
    """)).fetchall()
    print(f"{'品种':<8}{'天数':>6}{'有15:00':>9}{'完全相等':>10}{'差>0.1%':>10}")
    print("-" * 96)
    tot_off = 0
    for r in rows[:40]:
        m = r._mapping
        tot_off += m["off"]
        print(f"{m['symbol']:<8}{m['n']:>6}{m['has15']:>9}{m['exact']:>10}{m['off']:>10}")
    print("-" * 96)
    print("全库合计『daily 与 hourly-15:00 不一致』天数:", tot_off)
