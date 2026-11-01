# -*- coding: utf-8 -*-
"""分层 schema 对比：public vs l0_raw vs l3_ref 哪个好些是权威。"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, **kw):
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    try:
        rows = sess.execute(text(sql), kw).fetchall()
        if not rows:
            print("(空)")
            return
        cols = list(rows[0]._mapping.keys())
        print(" | ".join(str(c) for c in cols))
        print("-" * 78)
        for r in rows[:60]:
            print(" | ".join(str(x) for x in r))
        if len(rows) > 60:
            print("... 共 %d 行，仅显示前 60" % len(rows))
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e)[:300])


with session_scope() as s:
    show(s, "dim_trading_cost 各 schema 行数", """
        SELECT 'public' AS sch, count(*) n, count(DISTINCT variety_code) n_var
        FROM public.dim_trading_cost
        UNION ALL SELECT 'l3_ref', count(*), count(DISTINCT variety_code)
        FROM l3_ref.dim_trading_cost
    """)

    show(s, "l3_ref.dim_trading_cost 样例", """
        SELECT * FROM l3_ref.dim_trading_cost LIMIT 6
    """)

    show(s, "各 schema hourly_bar 行数与时间范围", """
        SELECT 'public' AS sch, count(*) n, count(DISTINCT symbol) n_sym,
               count(DISTINCT src) n_src, min(ts) mn, max(ts) mx
        FROM public.hourly_bar
        UNION ALL
        SELECT 'l0_raw', count(*), count(DISTINCT symbol), count(DISTINCT src),
               min(ts), max(ts) FROM l0_raw.hourly_bar
    """)

    show(s, "各 schema daily_bar 行数与时间范围", """
        SELECT 'public' AS sch, count(*) n, count(DISTINCT symbol) n_sym,
               count(DISTINCT src) n_src, min(trade_date) mn, max(trade_date) mx
        FROM public.daily_bar
        UNION ALL
        SELECT 'l0_raw', count(*), count(DISTINCT symbol), count(DISTINCT src),
               min(trade_date), max(trade_date) FROM l0_raw.daily_bar
    """)

    show(s, "l1_mkt / l2_adj 层级样例（bar_60m）", """
        SELECT 'l1_mkt.bar_60m' t, count(*) n, count(DISTINCT symbol) n_sym,
               min(ts) mn, max(ts) mx FROM l1_mkt.bar_60m
        UNION ALL SELECT 'l2_adj.bar_60m_ba', count(*), count(DISTINCT symbol),
               min(ts), max(ts) FROM l2_adj.bar_60m_ba
        UNION ALL SELECT 'l1_mkt.bar_15m', count(*), count(DISTINCT symbol),
               min(ts), max(ts) FROM l1_mkt.bar_15m
        UNION ALL SELECT 'l1_mkt.bar_5m', count(*), count(DISTINCT symbol),
               min(ts), max(ts) FROM l1_mkt.bar_5m
    """)

    show(s, "public.daily_bar 是否 VIEW", """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_name IN ('daily_bar','hourly_bar','dim_trading_cost')
        ORDER BY table_name, table_schema
    """)
