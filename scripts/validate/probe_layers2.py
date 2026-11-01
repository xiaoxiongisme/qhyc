# -*- coding: utf-8 -*-
"""列名 + 分层对照。"""
from sqlalchemy import text
from app.core.db import session_scope


def q(sess, sql, show_rows=True, limit=40):
    try:
        rows = sess.execute(text(sql)).fetchall()
        return rows
    except Exception as e:
        sess.rollback()
        print("  ERROR:", type(e).__name__, str(e)[:200])
        return None


def show(sess, label, sql, limit=40):
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    rows = q(sess, sql)
    if rows is None:
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
    show(s, "hourly_bar 列名", """
        SELECT table_schema, column_name, data_type
        FROM information_schema.columns
        WHERE table_name='hourly_bar'
        ORDER BY table_schema, ordinal_position
    """)

    show(s, "daily_bar 列名", """
        SELECT table_schema, column_name, data_type
        FROM information_schema.columns
        WHERE table_name='daily_bar'
        ORDER BY table_schema, ordinal_position
    """)

    show(s, "l1_mkt.bar_60m 列名", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema='l1_mkt' AND table_name='bar_60m'
        ORDER BY ordinal_position
    """)

    show(s, "public / l0_raw hourly_bar 行数", """
        SELECT 'public' sch, count(*) n, count(DISTINCT symbol) n_sym,
               count(DISTINCT src) n_src, min(dt) mn, max(dt) mx
        FROM public.hourly_bar
    """)
    show(s, "l0_raw.hourly_bar", """
        SELECT 'l0_raw' sch, count(*) n, count(DISTINCT symbol) n_sym,
               count(DISTINCT src) n_src, min(dt) mn, max(dt) mx
        FROM l0_raw.hourly_bar
    """)

    show(s, "public.daily_bar 覆盖", """
        SELECT count(*) n, count(DISTINCT symbol) n_sym, count(DISTINCT src) n_src,
               min(trade_date) mn, max(trade_date) mx
        FROM public.daily_bar
    """)
    show(s, "l0_raw.daily_bar 覆盖", """
        SELECT count(*) n, count(DISTINCT symbol) n_sym, count(DISTINCT src) n_src,
               min(trade_date) mn, max(trade_date) mx
        FROM l0_raw.daily_bar
    """)

    show(s, "l1_mkt / l2_adj 覆盖", """
        SELECT 'l1_mkt.bar_60m' t, count(*) n, count(DISTINCT symbol) n_sym,
               min(ts) mn, max(ts) mx FROM l1_mkt.bar_60m
    """)
    show(s, "l2_adj.bar_60m_ba", """
        SELECT 'l2_adj.bar_60m_ba' t, count(*) n, count(DISTINCT symbol) n_sym,
               min(ts) mn, max(ts) mx FROM l2_adj.bar_60m_ba
    """)
    show(s, "l1_mkt.bar_15m", """
        SELECT 'l1_mkt.bar_15m' t, count(*) n, count(DISTINCT symbol) n_sym,
               min(ts) mn, max(ts) mx FROM l1_mkt.bar_15m
    """)
    show(s, "l1_mkt.bar_5m", """
        SELECT 'l1_mkt.bar_5m' t, count(*) n, count(DISTINCT symbol) n_sym,
               min(ts) mn, max(ts) mx FROM l1_mkt.bar_5m
    """)

    show(s, "表类型（是否 VIEW）", """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_name IN ('daily_bar','hourly_bar','dim_trading_cost','minute_bar')
        ORDER BY table_name, table_schema
    """)
