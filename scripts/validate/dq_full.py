# -*- coding: utf-8 -*-
"""全库数据质量体检：OHLC 一致性、异常值、零成交、重复、停更。"""
from sqlalchemy import text
from app.core.db import session_scope


def show(sess, label, sql, limit=40):
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
        print("(无 —— 通过)")
        return
    cols = list(rows[0]._mapping.keys())
    print(" | ".join(str(c) for c in cols))
    print("-" * 96)
    for r in rows[:limit]:
        print(" | ".join(str(x) for x in r))
    if len(rows) > limit:
        print("... 共 %d 行" % len(rows))


TB = "l0_raw.hourly_bar"

with session_scope() as s:
    show(s, "Q1 空值检查（各列 NULL 数）", f"""
        SELECT count(*) total,
               count(*) FILTER (WHERE open  IS NULL) o_null,
               count(*) FILTER (WHERE high IS NULL) h_null,
               count(*) FILTER (WHERE low  IS NULL) l_null,
               count(*) FILTER (WHERE close IS NULL) c_null,
               count(*) FILTER (WHERE volume IS NULL) v_null,
               count(*) FILTER (WHERE symbol IS NULL OR symbol='') s_null
        FROM {TB}
    """)

    show(s, "Q2 OHLC 一致性违规（high < low 或 high < greatest(o,c) 或 low > least(o,c)）", f"""
        SELECT symbol, count(*) n, min(trade_datetime)::date mn, max(trade_datetime)::date mx
        FROM {TB}
        WHERE high < low OR high < GREATEST(open, close) OR low > LEAST(open, close)
        GROUP BY 1 ORDER BY n DESC
    """)

    show(s, "Q3 非正价格（<=0）", f"""
        SELECT symbol, count(*) n, min(trade_datetime)::date mn, max(trade_datetime)::date mx,
               min(close) min_close
        FROM {TB}
        WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
        GROUP BY 1 ORDER BY n DESC
    """)

    show(s, "Q4 负成交量 / 负持仓", f"""
        SELECT symbol, count(*) n FROM {TB}
        WHERE volume < 0 OR oi < 0 GROUP BY 1 ORDER BY n DESC
    """)

    show(s, "Q5 零成交 bar（volume=0，可能是停牌或虚假插入）", f"""
        SELECT symbol, count(*) n, min(trade_datetime)::date mn, max(trade_datetime)::date mx
        FROM {TB} WHERE volume = 0 GROUP BY 1 ORDER BY n DESC
    """, limit=25)

    show(s, "Q6 bar 内零振幅（open=high=low=close，且成交量为0）", f"""
        SELECT symbol, count(*) n FROM {TB}
        WHERE open=high AND high=low AND low=close AND volume=0
        GROUP BY 1 ORDER BY n DESC
    """, limit=25)

    show(s, "Q7 极端跳变（相邻 bar 收盘价变动 > 20%）", f"""
        SELECT symbol, trade_datetime, close, prev_close, pct FROM (
            SELECT symbol, trade_datetime, close,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY trade_datetime) prev_close,
                   ABS(close - LAG(close) OVER (PARTITION BY symbol ORDER BY trade_datetime))
                     / NULLIF(LAG(close) OVER (PARTITION BY symbol ORDER BY trade_datetime), 0) pct
            FROM {TB}
        ) x WHERE pct > 0.20 AND prev_close > 0
        ORDER BY pct DESC
    """, limit=25)

    show(s, "Q8 未来数据（trade_datetime 晚于现在）", f"""
        SELECT symbol, count(*) n, max(trade_datetime) mx FROM {TB}
        WHERE trade_datetime > NOW() GROUP BY 1 ORDER BY n DESC
    """)

    show(s, "Q9 重复 (symbol, trade_datetime, src)", f"""
        SELECT count(*) FROM (
            SELECT symbol, trade_datetime, src FROM {TB}
            GROUP BY 1,2,3 HAVING count(*) > 1
        ) t
    """)

    show(s, "Q10 daily_bar 与 hourly_bar 收盘价一致性抽查（最近30交易日）", """
        SELECT d.symbol, count(*) n_days,
               count(*) FILTER (WHERE ABS(d.close - h.hclose) > d.close * 0.02) mismatch
        FROM (
            SELECT symbol, trade_date, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) rn
            FROM l0_raw.daily_bar WHERE src='akshare'
        ) d
        JOIN LATERAL (
            SELECT close hclose FROM l0_raw.hourly_bar h
            WHERE h.symbol = d.symbol AND h.trade_datetime::date = d.trade_date
            ORDER BY h.trade_datetime DESC LIMIT 1
        ) h ON TRUE
        WHERE d.rn <= 30
        GROUP BY 1 ORDER BY mismatch DESC
    """, limit=60)
