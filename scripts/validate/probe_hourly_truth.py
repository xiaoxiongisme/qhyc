# -*- coding: utf-8 -*-
"""小时线缺失真相：按 src × 品种 × 年份 的覆盖矩阵。"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, limit=80):
    print("\n" + "=" * 92)
    print(label)
    print("=" * 92)
    try:
        rows = sess.execute(text(sql)).fetchall()
    except Exception as e:
        sess.rollback()
        print("  ERROR:", type(e).__name__, str(e)[:250])
        return
    if not rows:
        print("(空)")
        return
    cols = list(rows[0]._mapping.keys())
    print(" | ".join(str(c) for c in cols))
    print("-" * 92)
    for r in rows[:limit]:
        print(" | ".join(str(x) for x in r))
    if len(rows) > limit:
        print("... 共 %d 行" % len(rows))


with session_scope() as s:
    show(s, "hourly_bar 按 src × 年份 的行数与品种数", """
        SELECT src, EXTRACT(YEAR FROM trade_datetime)::int yr,
               count(*) rows_cnt, count(DISTINCT symbol) sym_cnt,
               min(trade_datetime)::date mn, max(trade_datetime)::date mx
        FROM l0_raw.hourly_bar
        GROUP BY 1,2 ORDER BY 1,2
    """, limit=100)

    show(s, "minute_bar 列名", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema='l0_raw' AND table_name='minute_bar'
        ORDER BY ordinal_position
    """)

    show(s, "agg_1m 与 akshare 的品种集合差异", """
        SELECT symbol,
               MAX(CASE WHEN src='agg_1m' THEN 1 ELSE 0 END) has_agg,
               MAX(CASE WHEN src='akshare' THEN 1 ELSE 0 END) has_aks
        FROM l0_raw.hourly_bar GROUP BY 1 ORDER BY 1
    """, limit=80)

    show(s, "两个 src 重叠区（各品种的两个源起止）", """
        SELECT symbol, src, count(*) n,
               min(trade_datetime)::date mn, max(trade_datetime)::date mx
        FROM l0_raw.hourly_bar
        WHERE symbol IN ('CU888','AU888','RB888','AG888','A888','SM888','EB888','CF888','SC888')
        GROUP BY 1,2 ORDER BY 1,2
    """, limit=80)
