# -*- coding: utf-8 -*-
"""小时线真实缺口体检：合并所有 src 后，逐品种检查连续性。"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from sqlalchemy import text
from app.core.db import session_scope

with session_scope() as s:
    # 1) 合并所有 src 后的逐品种覆盖
    rows = s.execute(text("""
        SELECT symbol,
               min(trade_datetime) mn, max(trade_datetime) mx,
               count(*) n, count(DISTINCT src) n_src,
               count(DISTINCT trade_datetime::date) n_days
        FROM l0_raw.hourly_bar
        GROUP BY 1 ORDER BY 1
    """)).fetchall()

    print("=" * 96)
    print("合并全部 src 后的逐品种覆盖")
    print("=" * 96)
    print(f"{'品种':<8}{'起':<12}{'止':<12}{'bar数':>9}{'交易日':>8}{'源数':>5}{'bar/日':>8}")
    print("-" * 96)
    stats = []
    for r in rows:
        m = r._mapping
        mn, mx = m["mn"], m["mx"]
        days = m["n_days"]
        bpd = m["n"] / days if days else 0
        stats.append((m["symbol"], mn, mx, m["n"], days, bpd))
        print(f"{m['symbol']:<8}{str(mn.date()):<12}{str(mx.date()):<12}"
              f"{m['n']:>9}{days:>8}{m['n_src']:>5}{bpd:>8.1f}")

    # 2) 每年每品种是否有数据（覆盖率矩阵）
    print("\n" + "=" * 96)
    print("逐品种 × 年份 是否有数据（. = 无，数字 = 该年 bar 数/1000）")
    print("=" * 96)
    yr_rows = s.execute(text("""
        SELECT symbol, EXTRACT(YEAR FROM trade_datetime)::int yr, count(*) n
        FROM l0_raw.hourly_bar GROUP BY 1,2 ORDER BY 1,2
    """)).fetchall()
    yr_map = defaultdict(dict)
    for r in yr_rows:
        m = r._mapping
        yr_map[m["symbol"]][m["yr"]] = m["n"]
    years = sorted({y for v in yr_map.values() for y in v})
    print("品种    " + "".join(f"{str(y)[2:]:>5}" for y in years))
    print("-" * 96)
    for sym in sorted(yr_map):
        line = f"{sym:<8}"
        for y in years:
            n = yr_map[sym].get(y, 0)
            line += f"{n/1000:>5.0f}" if n else f"{'.':>5}"
        print(line)

    # 3) 真空 gap：相邻交易日之间间隔 > 5 天（排除长假）的空洞
    print("\n" + "=" * 96)
    print("逐品种「交易日级」缺口（相邻交易日间隔 > 7 天，排除春节/国庆长假）")
    print("=" * 96)
    gap_rows = s.execute(text("""
        SELECT symbol, d, gap FROM (
            SELECT symbol, d::date AS d,
                   d::date - LAG(d::date) OVER (PARTITION BY symbol ORDER BY d) AS gap
            FROM (SELECT DISTINCT symbol, trade_datetime::date AS d
                  FROM l0_raw.hourly_bar) t
        ) x WHERE gap > 7 ORDER BY symbol, d
    """)).fetchall()
    if not gap_rows:
        print("(无 >7 天缺口)")
    else:
        cur = None
        cnt = 0
        for r in gap_rows:
            m = r._mapping
            if cur != m["symbol"]:
                cur = m["symbol"]
                print(f"\n[{cur}]")
            cnt += 1
            print(f"   {m['d']}  前一日间隔 {m['gap']} 天")
        print(f"\n合计缺口 {cnt} 处")

    # 4) 每个品种每年应有交易日数（用 <=6.7bar/日 判定残缺）
    print("\n" + "=" * 96)
    print("年度稀疏告警：某年该品种 bar/交易日 < 4（低于正常 6.7 的一半以上）")
    print("=" * 96)
    sparse = s.execute(text("""
        SELECT symbol, EXTRACT(YEAR FROM trade_datetime)::int yr,
               count(*) n, count(DISTINCT trade_datetime::date) days
        FROM l0_raw.hourly_bar GROUP BY 1,2
        HAVING count(*)::float / count(DISTINCT trade_datetime::date) < 4
        ORDER BY 1,2
    """)).fetchall()
    if not sparse:
        print("(无)")
    for r in sparse:
        m = r._mapping
        print(f"  {m['symbol']} {m['yr']}: {m['n']} bar / {m['days']} 日 "
              f"= {m['n']/m['days']:.2f} bar/日")
