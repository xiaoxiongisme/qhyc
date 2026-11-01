# -*- coding: utf-8 -*-
"""用正确列名做分层覆盖对照。"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, limit=40):
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    try:
        rows = sess.execute(text(sql)).fetchall()
    except Exception as e:
        sess.rollback()
        print("  ERROR:", type(e).__name__, str(e)[:200])
        return
    if not rows:
        print("(空)")
        return
    cols = list(rows[0]._mapping.keys())
    print(" | ".join(str(c) for c in cols))
    print("-" * 78)
    for r in rows[:limit]:
        print(" | ".join(str(x) for x in r))
    if len(rows) > limit:
        print("... 共 %d 行" % len(rows))


with session_scope() as s:
    for sch in ("public", "l0_raw"):
        show(s, "%s.hourly_bar 覆盖" % sch, """
            SELECT '%s' sch, count(*) n, count(DISTINCT symbol) n_sym,
                   count(DISTINCT src) n_src, min(trade_datetime) mn,
                   max(trade_datetime) mx
            FROM %s.hourly_bar
        """ % (sch, sch))

    for sch in ("public", "l0_raw"):
        show(s, "%s.daily_bar 覆盖" % sch, """
            SELECT '%s' sch, count(*) n, count(DISTINCT symbol) n_sym,
                   count(DISTINCT src) n_src, min(trade_date) mn,
                   max(trade_date) mx
            FROM %s.daily_bar
        """ % (sch, sch))

    show(s, "public.daily_bar 按 src 分布", """
        SELECT src, count(*) n, count(DISTINCT symbol) n_sym,
               min(trade_date) mn, max(trade_date) mx
        FROM public.daily_bar GROUP BY 1 ORDER BY n DESC
    """)
    show(s, "l0_raw.daily_bar 按 src 分布", """
        SELECT src, count(*) n, count(DISTINCT symbol) n_sym,
               min(trade_date) mn, max(trade_date) mx
        FROM l0_raw.daily_bar GROUP BY 1 ORDER BY n DESC
    """)
    show(s, "public.hourly_bar 按 src 分布", """
        SELECT src, count(*) n, count(DISTINCT symbol) n_sym,
               min(trade_datetime) mn, max(trade_datetime) mx
        FROM public.hourly_bar GROUP BY 1 ORDER BY n DESC
    """)
    show(s, "l0_raw.hourly_bar 按 src 分布", """
        SELECT src, count(*) n, count(DISTINCT symbol) n_sym,
               min(trade_datetime) mn, max(trade_datetime) mx
        FROM l0_raw.hourly_bar GROUP BY 1 ORDER BY n DESC
    """)

    for tb in ("bar_60m", "bar_30m", "bar_15m", "bar_5m"):
        show(s, "l1_mkt.%s 覆盖" % tb, """
            SELECT 'l1_mkt.%(tb)s' t, count(*) n, count(DISTINCT symbol) n_sym,
                   min(bucket) mn, max(bucket) mx
            FROM l1_mkt.%(tb)s
        """ % {"tb": tb})

    show(s, "l2_adj.bar_60m_ba 覆盖", """
        SELECT 'l2_adj.bar_60m_ba' t, count(*) n, count(DISTINCT symbol) n_sym,
               min(bucket) mn, max(bucket) mx FROM l2_adj.bar_60m_ba
    """)

    show(s, "l0_raw.minute_bar 覆盖", """
        SELECT count(*) n, count(DISTINCT symbol) n_sym,
               min(trade_datetime) mn, max(trade_datetime) mx
        FROM l0_raw.minute_bar
    """)

    show(s, "表类型（VIEW vs BASE TABLE）", """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_name IN ('daily_bar','hourly_bar','dim_trading_cost','minute_bar')
        ORDER BY table_name, table_schema
    """)
