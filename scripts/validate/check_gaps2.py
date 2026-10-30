# -*- coding: utf-8 -*-
"""缺口按月分布 + 停更品种 + 日内 bar 数分布。"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, limit=100):
    print("\n" + "=" * 88)
    print(label)
    print("=" * 88)
    try:
        rows = sess.execute(text(sql)).fetchall()
    except Exception as e:
        sess.rollback()
        print("  ERROR:", type(e).__name__, str(e)[:250])
        return None
    if not rows:
        print("(空)")
        return None
    cols = list(rows[0]._mapping.keys())
    print(" | ".join(str(c) for c in cols))
    print("-" * 88)
    for r in rows[:limit]:
        print(" | ".join(str(x) for x in r))
    if len(rows) > limit:
        print("... 共 %d 行" % len(rows))
    return rows


with session_scope() as s:
    show(s, "缺口按月分布（若集中在 2月/10月 = 春节国庆，属正常）", """
        SELECT EXTRACT(MONTH FROM d)::int mon, count(*) cnt,
               min(gap) min_gap, max(gap) max_gap
        FROM (
            SELECT symbol, d, gap FROM (
                SELECT symbol, d::date AS d,
                       d::date - LAG(d::date) OVER (PARTITION BY symbol ORDER BY d) AS gap
                FROM (SELECT DISTINCT symbol, trade_datetime::date AS d
                      FROM l0_raw.hourly_bar) t
            ) x WHERE gap > 7
        ) y GROUP BY 1 ORDER BY 1
    """)

    show(s, "非 2月/10月 的异常缺口（重点审查）", """
        SELECT symbol, d, gap FROM (
            SELECT symbol, d::date AS d,
                   d::date - LAG(d::date) OVER (PARTITION BY symbol ORDER BY d) AS gap
            FROM (SELECT DISTINCT symbol, trade_datetime::date AS d
                  FROM l0_raw.hourly_bar) t
        ) x
        WHERE gap > 7 AND EXTRACT(MONTH FROM d)::int NOT IN (2, 10)
        ORDER BY gap DESC, symbol
    """, limit=60)

    show(s, "各品种最后更新时间（停更检测）", """
        SELECT symbol, max(trade_datetime)::date last_day,
               count(DISTINCT src) n_src,
               string_agg(DISTINCT src, ',') srcs,
               (CURRENT_DATE - max(trade_datetime)::date) stale_days
        FROM l0_raw.hourly_bar
        GROUP BY 1
        HAVING max(trade_datetime)::date < CURRENT_DATE - 7
        ORDER BY last_day
    """, limit=30)

    show(s, "日内 bar 数分布（每交易日应有 6.7 根左右）", """
        SELECT bars_per_day, count(*) AS n_days FROM (
            SELECT symbol, trade_datetime::date d, count(*) bars_per_day
            FROM l0_raw.hourly_bar
            WHERE trade_datetime >= '2024-01-01'
            GROUP BY 1,2
        ) t GROUP BY 1 ORDER BY 1
    """, limit=30)

    show(s, "2024年后 每交易日 bar 数异常的品种（<4 或 >12）", """
        SELECT symbol, trade_datetime::date d, count(*) n
        FROM l0_raw.hourly_bar
        WHERE trade_datetime >= '2024-01-01'
        GROUP BY 1,2
        HAVING count(*) < 4 OR count(*) > 12
        ORDER BY 1,2
    """, limit=60)

    show(s, "2024年后 每交易日 bar 数 <4 的品种统计", """
        SELECT symbol, count(*) AS bad_days, min(c) mn, max(c) mx FROM (
            SELECT symbol, trade_datetime::date d, count(*) c
            FROM l0_raw.hourly_bar
            WHERE trade_datetime >= '2024-01-01'
            GROUP BY 1,2 HAVING count(*) < 4
        ) t GROUP BY 1 ORDER BY bad_days DESC
    """, limit=40)
