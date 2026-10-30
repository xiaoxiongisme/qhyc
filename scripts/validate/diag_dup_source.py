# -*- coding: utf-8 -*-
"""查清「数据源重复」的真实形态。

区分三种情形：
  A. 同一张物理表里多个 src（行级多源）
  B. 多张物理表（不同表存同源数据）
  C. public schema 的 VIEW（看到多张，实为同义词）
"""
import re

from sqlalchemy import text
from app.core.db import session_scope

SEP = "=" * 78


def q(s, sql, **kw):
    try:
        return s.execute(text(sql), kw).fetchall()
    except Exception as e:
        return [("ERR", str(e).replace("\n", " ")[:170])]


print(SEP)
print("一、所有含 bar 的物理表 vs 视图（跨 schema）")
print(SEP)
with session_scope() as s:
    rows = q(s, """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_name LIKE '%bar%'
        ORDER BY table_schema, table_name
    """)
for sc, tb, ty in rows:
    print(f"  {sc:<10} {tb:<24} {ty}")

print()
print(SEP)
print("二、public schema 的 VIEW 实际指向哪张真表")
print(SEP)
with session_scope() as s:
    rows = q(s, """
        SELECT schemaname, viewname, definition
        FROM pg_views
        WHERE schemaname='public' AND viewname LIKE '%bar%'
        ORDER BY viewname
    """)
for sc, vn, dfn in rows:
    m = re.search(r"FROM\s+([a-z0-9_]+\.[a-z0-9_]+)", dfn, re.I)
    print(f"  public.{vn:<20} -> {m.group(1) if m else dfn[:80]}")

print()
print(SEP)
print("三、l0_raw 三个周期表的 src 分布（真实行数）")
print(SEP)
with session_scope() as s:
    for tb, col in (("hourly_bar", "trade_datetime"),
                    ("daily_bar", "trade_date"),
                    ("minute_bar", "ts")):
        rows = q(s, f"""
            SELECT src, count(*) n, min({col})::date mn, max({col})::date mx,
                   count(DISTINCT symbol) syms
            FROM l0_raw.{tb} GROUP BY 1 ORDER BY n DESC
        """)
        print(f"  [l0_raw.{tb}]")
        for r in rows:
            print(f"     src={r[0]:<14} rows={r[1]:>12,}  {r[2]} ~ {r[3]}  品种={r[4]}")

print()
print(SEP)
print("四、hourly_bar 的 agg_1m 与 akshare 有多少 (symbol,ts) 双份重叠")
print(SEP)
with session_scope() as s:
    rows = q(s, """
        SELECT count(*) FROM (
          SELECT symbol, trade_datetime FROM l0_raw.hourly_bar
          WHERE src IN ('agg_1m','akshare')
          GROUP BY 1,2 HAVING count(DISTINCT src) > 1
        ) x
    """)
    print("  双源重叠组合数:", rows[0][0] if rows else "?")
    for r in q(s, """
        SELECT src, count(*) FROM l0_raw.hourly_bar
        WHERE src IN ('agg_1m','akshare') GROUP BY 1
    """):
        print(f"    {r[0]:<12} {r[1]:>12,}")

print()
print(SEP)
print("五、minute_bar 的 src 分布（最上游）")
print(SEP)
with session_scope() as s:
    for r in q(s, """
        SELECT src, count(*) n, max(ts)::date mx, count(DISTINCT symbol) syms
        FROM l0_raw.minute_bar GROUP BY 1 ORDER BY n DESC
    """):
        print(f"    {r[0]:<16} rows={r[1]:>14,}  最新={r[2]}  品种={r[3]}")

print()
print(SEP)
print("六、所有真实物理表（判断是否存在『内容同源的多张表』）")
print(SEP)
with session_scope() as s:
    for r in q(s, """
        SELECT table_schema||'.'||table_name AS full_name
        FROM information_schema.tables
        WHERE table_type='BASE TABLE'
          AND (table_name LIKE '%hourly%' OR table_name LIKE '%minute%'
               OR table_name LIKE '%daily%' OR table_name LIKE '%kline%'
               OR table_name LIKE '%bar%')
        ORDER BY 1
    """):
        print("   ", r[0])

print()
print(SEP)
print("七、daily_bar 是否也存在多源重复")
print(SEP)
with session_scope() as s:
    rows = q(s, """
        SELECT count(*) FROM (
          SELECT symbol, trade_date FROM l0_raw.daily_bar
          GROUP BY 1,2 HAVING count(DISTINCT src) > 1
        ) x
    """)
    print("  daily_bar 多源重叠组合数:", rows[0][0] if rows else "?")
    rows = q(s, """
        SELECT count(*) FROM (
          SELECT symbol, trade_date, src FROM l0_raw.daily_bar
          GROUP BY 1,2,3 HAVING count(*) > 1
        ) x
    """)
    print("  daily_bar 完全重复键组合数:", rows[0][0] if rows else "?")
