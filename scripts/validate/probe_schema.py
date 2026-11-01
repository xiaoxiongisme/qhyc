# -*- coding: utf-8 -*-
"""数据源/费率表层结构探测。"""
import sys
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
            return rows
        cols = list(rows[0]._mapping.keys())
        print(" | ".join(str(c) for c in cols))
        print("-" * 78)
        for r in rows:
            print(" | ".join(str(x) for x in r))
        return rows
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e)[:300])
        return None


with session_scope() as s:
    # 1) 所有含 bar / cost / trading 的表
    show(s, "所有相关表", """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
          AND (table_name ILIKE '%bar%' OR table_name ILIKE '%cost%'
               OR table_name ILIKE '%contract%' OR table_name ILIKE '%symbol%'
               OR table_name ILIKE '%dim%')
        ORDER BY table_schema, table_name
    """)

    show(s, "dim_trading_cost 列结构", """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'dim_trading_cost'
        ORDER BY ordinal_position
    """)

    show(s, "dim_trading_cost 行数", """
        SELECT count(*) AS n, count(DISTINCT symbol) AS n_sym
        FROM dim_trading_cost
    """)

    show(s, "l0_raw.daily_bar 列结构", """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='l0_raw' AND table_name='daily_bar'
        ORDER BY ordinal_position
    """)

    show(s, "hourly_bar 所在 schema + 列结构", """
        SELECT table_schema, column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'hourly_bar'
        ORDER BY table_schema, ordinal_position
    """)
