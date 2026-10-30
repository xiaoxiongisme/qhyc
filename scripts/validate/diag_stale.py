# -*- coding: utf-8 -*-
"""停更根因：7 个品种为何卡在 2026-06-12？"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, limit=30):
    print("\n" + "=" * 96)
    print(label)
    print("=" * 96)
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
    print("-" * 96)
    for r in rows[:limit]:
        print(" | ".join(str(x) for x in r))
    if len(rows) > limit:
        print("... 共 %d 行" % len(rows))


STALE = ("BR888", "CJ888", "EC888", "LU888", "PF888", "PG888", "PK888")

with session_scope() as s:
    show(s, "minute_bar 的 src 分布与最后更新（agg_1m 的上游）", """
        SELECT src, count(*) n, count(DISTINCT symbol) n_sym,
               min(ts)::date mn, max(ts)::date mx
        FROM l0_raw.minute_bar GROUP BY 1 ORDER BY mx DESC
    """)

    show(s, "minute_bar 停更品种明细", """
        SELECT symbol, src, count(*) n, min(ts)::date mn, max(ts)::date mx
        FROM l0_raw.minute_bar
        WHERE symbol = ANY(:ss)
        GROUP BY 1,2 ORDER BY 1
    """.replace(":ss", "ARRAY[%s]" % ",".join("'%s'" % x for x in STALE)))

    show(s, "对照：正常品种在 minute_bar 的更新情况", """
        SELECT symbol, src, count(*) n, max(ts)::date mx
        FROM l0_raw.minute_bar
        WHERE symbol IN ('RB888','CU888','AU888','AG888')
        GROUP BY 1,2 ORDER BY 1
    """)

    show(s, "minute_bar 全品种最后更新时间分布（看是否整体停摆）", """
        SELECT max(ts)::date last_day, count(*) n_sym
        FROM l0_raw.minute_bar GROUP BY 1 ORDER BY 1 DESC
    """, limit=25)

    show(s, "l1_mkt.bar_60m 是否覆盖这7个品种（可能是聚合层的问题）", """
        SELECT symbol, count(*) n, min(bucket)::date mn, max(bucket)::date mx
        FROM l1_mkt.bar_60m
        WHERE symbol = ANY(ARRAY[%s])
        GROUP BY 1 ORDER BY 1
    """ % ",".join("'%s'" % x for x in STALE))

    show(s, "l1_mkt.bar_60m 全品种最后更新分布", """
        SELECT max(bucket)::date last_day, count(*) n_sym
        FROM l1_mkt.bar_60m GROUP BY 1 ORDER BY 1 DESC
    """, limit=25)
