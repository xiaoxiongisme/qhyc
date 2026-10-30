# -*- coding: utf-8 -*-
"""复核 SM888 候选资格 —— 重点查 2026 条零成交 bar 是否会污染回测。

背景：SM(锰硅) 在昨天的「6 品种候选池」里，但数据质量体检发现它有
2026 条零成交 bar。需判断：
  1. 这些零成交 bar 分布在什么年份/是否在回测统计区间内
  2. 去掉它们后 SM 的 edge 是否还在
  3. 同时复核候选池其余品种的零成交占比
"""
import numpy as np
from sqlalchemy import text
from app.core.db import session_scope

SEP = "=" * 78
CAND = ["EB", "CU", "CF", "SC", "A", "SM"]


def sym_of(v):
    return v + "888"


with session_scope() as s:
    print(SEP)
    print("一、候选池品种的零成交 bar 分布（daily_bar, src=akshare）")
    print(SEP)
    print(f"{'品种':<6}{'总bar':>8}{'零成交':>8}{'占比':>8}  {'零成交年份分布':<40}")
    for v in CAND:
        rows = s.execute(text("""
            SELECT EXTRACT(YEAR FROM trade_date)::int y, count(*) tot,
                   SUM(CASE WHEN COALESCE(volume,0) <= 0 THEN 1 ELSE 0 END) z
            FROM l0_raw.daily_bar
            WHERE symbol = :s AND src = 'akshare'
            GROUP BY 1 ORDER BY 1
        """), {"s": sym_of(v)}).fetchall()
        if not rows:
            print(f"{v:<6}{'N/A':>8}")
            continue
        tot = sum(r[1] for r in rows)
        z = sum(r[2] or 0 for r in rows)
        dist = ", ".join(f"{r[0]}:{r[2]}" for r in rows if (r[2] or 0) > 0)
        print(f"{v:<6}{tot:>8}{z:>8}{z/max(tot,1)*100:>7.1f}%  {dist[:38]:<40}")

    print()
    print(SEP)
    print("二、SM888 零成交 bar 是否落在回测区间 2021-01-01~2026-10-30")
    print(SEP)
    rows = s.execute(text("""
        SELECT EXTRACT(YEAR FROM trade_date)::int y, count(*) z
        FROM l0_raw.daily_bar
        WHERE symbol='SM888' AND src='akshare'
          AND COALESCE(volume,0) <= 0
          AND trade_date >= DATE '2021-01-01'
        GROUP BY 1 ORDER BY 1
    """)).fetchall()
    tot2021 = sum(r[1] for r in rows)
    print("  2021 之后的零成交 bar 数:", tot2021)
    for r in rows:
        print(f"    {r[0]}: {r[1]}")
    if not rows:
        print("    (无) —— 零成交全部集中在 2021 年之前")

    print()
    print(SEP)
    print("三、SM888 零成交 bar 的价格是否恒定（若是，说明是填充行）")
    print(SEP)
    rows = s.execute(text("""
        SELECT symbol, trade_date, open, high, low, close, volume, src
        FROM l0_raw.daily_bar
        WHERE symbol='SM888' AND COALESCE(volume,0) <= 0
        ORDER BY trade_date LIMIT 6
    """)).fetchall()
    for r in rows:
        print(f"    {r[1]} O={r[2]} H={r[3]} L={r[4]} C={r[5]} V={r[6]} src={r[7]}")

    print()
    print(SEP)
    print("四、SM888 首笔非零成交的日期（品种实际上市/开始有数据的时间）")
    print(SEP)
    r = s.execute(text("""
        SELECT min(trade_date), max(trade_date), count(*)
        FROM l0_raw.daily_bar
        WHERE symbol='SM888' AND src='akshare' AND COALESCE(volume,0) > 0
    """)).first()
    print(f"    非零成交区间: {r[0]} ~ {r[1]}  共 {r[2]} 根")
    r0 = s.execute(text("""
        SELECT min(trade_date), max(trade_date), count(*)
        FROM l0_raw.daily_bar WHERE symbol='SM888' AND src='akshare'
    """)).first()
    print(f"    全部行区间:   {r0[0]} ~ {r0[1]}  共 {r0[2]} 根")
