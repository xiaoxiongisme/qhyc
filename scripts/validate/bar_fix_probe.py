# -*- coding: utf-8 -*-
"""修复前探查（修正版）：minute_bar 列名为 ts。"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope


def main() -> int:
    with session_scope() as s:
        print("=== minute_bar 覆盖（按 src）===")
        rows = s.execute(text("""
            SELECT src, count(*) n, count(DISTINCT symbol) nsym,
                   min(ts)::date f, max(ts)::date l
            FROM l0_raw.minute_bar GROUP BY src ORDER BY src
        """)).fetchall()
        for r in rows:
            print(f"  src={r[0]:<10} rows={r[1]:>13,}  品种={r[2]:>3}  {r[3]} ~ {r[4]}")

        print("\n=== l1_mkt.bar_60m / bar_30m 覆盖 ===")
        for t in ("bar_60m", "bar_30m"):
            try:
                rows = s.execute(text(f"""
                    SELECT count(*) n, count(DISTINCT symbol) nsym,
                           min(bucket)::date f, max(bucket)::date l
                    FROM l1_mkt.{t}
                """)).fetchall()
                r = rows[0]
                print(f"  {t}: rows={r[0]:>12,}  品种={r[1]:>3}  {r[2]} ~ {r[3]}")
            except Exception as e:  # noqa: BLE001
                print(f"  {t}: 不可用 {type(e).__name__}: {str(e)[:100]}")

        print("\n=== SC888 单个交易日形态对照 ===")
        for day in ("2026-10-28", "2023-06-15"):
            print(f"\n  --- {day} ---")
            rows = s.execute(text("""
                SELECT src, to_char(min(ts),'HH24:MI') f, to_char(max(ts),'HH24:MI') l, count(*) n
                FROM l0_raw.minute_bar WHERE symbol='SC888' AND ts::date = :d
                GROUP BY src
            """), {"d": day}).fetchall()
            if not rows:
                print("     minute_bar: 无数据")
            for r in rows:
                print(f"     minute/{r[0]:<9} n={r[3]:>5}  {r[1]} ~ {r[2]}")

            rows = s.execute(text("""
                SELECT src, count(*) n,
                       string_agg(to_char(trade_datetime,'HH24:MI'),' ' ORDER BY trade_datetime) times
                FROM l0_raw.hourly_bar WHERE symbol='SC888' AND trade_datetime::date = :d
                GROUP BY src
            """), {"d": day}).fetchall()
            if not rows:
                print("     hourly_bar: 无数据")
            for r in rows:
                print(f"     hourly/{r[0]:<9} n={r[1]:>2}  {r[2]}")

            try:
                rows = s.execute(text("""
                    SELECT string_agg(to_char(bucket,'HH24:MI'),' ' ORDER BY bucket) times, count(*) n
                    FROM l1_mkt.bar_60m WHERE symbol='SC888' AND bucket::date = :d
                """), {"d": day}).fetchall()
                r = rows[0]
                print(f"     l1 60m      n={r[1]:>2}  {r[0] or '(无)'}")
            except Exception as e:  # noqa: BLE001
                print(f"     l1 60m: {type(e).__name__}")
            try:
                rows = s.execute(text("""
                    SELECT string_agg(to_char(bucket,'HH24:MI'),' ' ORDER BY bucket) times, count(*) n
                    FROM l1_mkt.bar_30m WHERE symbol='SC888' AND bucket::date = :d
                """), {"d": day}).fetchall()
                r = rows[0]
                print(f"     l1 30m      n={r[1]:>2}  {r[0] or '(无)'}")
            except Exception as e:  # noqa: BLE001
                print(f"     l1 30m: {type(e).__name__}")

        print("\n=== 全库：hourly_bar 各 src 的日均 bar 数（近 30 交易日，SC888）===")
        rows = s.execute(text("""
            SELECT src, round(avg(n)::numeric,2) avg_n, min(n) min_n, max(n) max_n, count(*) days
            FROM (
                SELECT src, trade_datetime::date d, count(*) n
                FROM l0_raw.hourly_bar
                WHERE symbol='SC888' AND trade_datetime >= DATE '2026-09-01'
                GROUP BY 1,2
            ) x GROUP BY src
        """)).fetchall()
        for r in rows:
            print(f"  src={r[0]:<9} 日均={r[1]}  最少={r[2]}  最多={r[3]}  天数={r[4]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
