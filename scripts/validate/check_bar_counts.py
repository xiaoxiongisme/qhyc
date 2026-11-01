# -*- coding: utf-8 -*-
"""追查日内 bar 数异常：AG888 为何 15 根/日？是否重复？"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, limit=60):
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
    show(s, "AG888 某日全部 bar（看是否为整点、有无重复）", """
        SELECT src, trade_datetime, open, high, low, close, volume
        FROM l0_raw.hourly_bar
        WHERE symbol='AG888' AND trade_datetime::date = '2026-05-12'
        ORDER BY trade_datetime, src
    """, limit=40)

    show(s, "AG888 对照：2026-05-11（15根异常前一天）", """
        SELECT src, trade_datetime, open, high, low, close, volume
        FROM l0_raw.hourly_bar
        WHERE symbol='AG888' AND trade_datetime::date = '2026-05-11'
        ORDER BY trade_datetime, src
    """, limit=40)

    show(s, "重复键检测：(symbol, trade_datetime, src) 是否有重复行", """
        SELECT symbol, count(*) AS dup_groups, sum(c-1) AS extra_rows FROM (
            SELECT symbol, trade_datetime, src, count(*) c
            FROM l0_raw.hourly_bar
            GROUP BY 1,2,3 HAVING count(*) > 1
        ) t GROUP BY 1 ORDER BY extra_rows DESC
    """, limit=40)

    show(s, "跨源重复检测：同一 (symbol, trade_datetime) 出现在多个 src", """
        SELECT symbol, count(*) AS overlap_bars FROM (
            SELECT symbol, trade_datetime
            FROM l0_raw.hourly_bar GROUP BY 1,2 HAVING count(DISTINCT src) > 1
        ) t GROUP BY 1 ORDER BY overlap_bars DESC
    """, limit=40)

    show(s, "AG888 逐月 bar/日（看结构何时变化）", """
        SELECT to_char(trade_datetime,'YYYY-MM') ym,
               count(*) n, count(DISTINCT trade_datetime::date) days,
               round(count(*)::numeric / count(DISTINCT trade_datetime::date), 2) bpd,
               count(DISTINCT src) n_src
        FROM l0_raw.hourly_bar
        WHERE symbol='AG888' AND trade_datetime >= '2025-01-01'
        GROUP BY 1 ORDER BY 1
    """, limit=30)

    show(s, "全部品种 2026年 bar/日（找普遍异常）", """
        SELECT symbol, round(count(*)::numeric/count(DISTINCT trade_datetime::date),2) bpd,
               count(DISTINCT trade_datetime::date) days
        FROM l0_raw.hourly_bar
        WHERE trade_datetime >= '2026-01-01'
        GROUP BY 1 ORDER BY bpd DESC
    """, limit=60)
