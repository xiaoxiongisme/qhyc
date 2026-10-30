# -*- coding: utf-8 -*-
"""查清 hourly_bar 各 src 的 bar 网格（J5）：相邻 bar 间隔 + 实际时刻序列。

起因：hourly_uni_join_check 的 J3 发现 agg_1m 日均 9.2 根、akshare 日均 7.2 根，
差约 28%。若两源「小时」的定义不同（一根=60 分钟 vs 按交易小节聚合），直接拼
hourly_bar_uni 会让 EMA140 / ATR14 在两段表达的时间跨度不一致 —— 参数含义漂移，
长周期回测结论不可信。本脚本把差异量化到可判定的程度。
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope

SAMPLE = ["RB888", "CU888", "AU888", "SC888", "A888"]


def main() -> int:
    with session_scope() as s:
        print("=== 相邻 bar 间隔分布（同交易日内，分钟）===")
        rows = s.execute(text("""
            WITH x AS (
                SELECT src, symbol, trade_datetime,
                       trade_datetime::date d,
                       lag(trade_datetime) OVER (
                           PARTITION BY symbol, src ORDER BY trade_datetime) prev
                FROM l0_raw.hourly_bar
                WHERE symbol = ANY(:syms)
            ), y AS (
                SELECT src, symbol,
                       extract(epoch FROM (trade_datetime - prev)) / 60.0 gap_min
                FROM x
                WHERE prev IS NOT NULL AND d = prev::date
            ), z AS (
                SELECT src, symbol, gap_min, count(*) n FROM y GROUP BY 1,2,3
            )
            SELECT src, gap_min, sum(n) tot,
                   round(sum(n) * 100.0 / sum(sum(n)) OVER (PARTITION BY src), 2) pct
            FROM z GROUP BY 1,2 ORDER BY 1, gap_min
        """), {"syms": SAMPLE}).fetchall()
        cur = None
        for r in rows:
            m = r._mapping
            if m["src"] != cur:
                cur = m["src"]
                print(f"\n  --- {cur} ---")
            print(f"    间隔 {float(m['gap_min']):>6.1f} 分  "
                  f"次数 {int(m['tot']):>9,}  占比 {float(m['pct']):>6.2f}%")

        print("\n=== 同一品种、同一交易日的实际时刻序列 ===")
        for sym, day in (("RB888", "2021-12-20"), ("RB888", "2023-06-15"),
                         ("CU888", "2021-12-20"), ("CU888", "2023-06-15"),
                         ("AU888", "2023-09-05"), ("AU888", "2023-09-07")):
            rr = s.execute(text("""
                SELECT src, string_agg(to_char(trade_datetime, 'HH24:MI'), ' '
                                       ORDER BY trade_datetime) times, count(*) n
                FROM l0_raw.hourly_bar
                WHERE symbol = :s AND trade_datetime::date = :d
                GROUP BY src ORDER BY src
            """), {"s": sym, "d": day}).fetchall()
            print(f"\n  {sym}  {day}")
            for x in rr:
                mm = x._mapping
                print(f"     {mm['src']:<9} n={mm['n']:>2}  {mm['times']}")

        print("\n=== 每日 bar 数分布（占比%）===")
        rows = s.execute(text("""
            WITH d AS (
                SELECT symbol, src, trade_datetime::date day, count(*) n
                FROM l0_raw.hourly_bar WHERE symbol = ANY(:syms)
                GROUP BY 1,2,3
            ), c AS (SELECT src, n, count(*) k FROM d GROUP BY 1,2)
            SELECT src, n, k, round(k*100.0/sum(k) OVER (PARTITION BY src),2) pct
            FROM c ORDER BY src, n
        """), {"syms": SAMPLE}).fetchall()
        cur = None
        for r in rows:
            m = r._mapping
            if m["src"] != cur:
                cur = m["src"]
                print(f"\n  --- {cur} ---")
            print(f"    每日 {int(m['n']):>3} 根   天数 {int(m['k']):>7,}  占比 {float(m['pct']):>6.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
