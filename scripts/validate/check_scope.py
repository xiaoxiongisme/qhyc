# -*- coding: utf-8 -*-
"""停更 7 品种是否在策略品种池内？补数可行性？"""
from sqlalchemy import text
from app.core.config import get_settings
from app.core.db import session_scope

STALE = ["BR888", "CJ888", "EC888", "LU888", "PF888", "PG888", "PK888"]

s_cfg = get_settings()
main = [x.symbol for x in s_cfg.main_contracts]
print("=" * 92)
print("融合策略 main_contracts 品种数:", len(main))
print("=" * 92)
main_set = set(main)
in_pool = [x for x in STALE if x in main_set]
out_pool = [x for x in STALE if x not in main_set]
print("停更 7 种中，【在策略池内】的:", in_pool if in_pool else "无 —— 不影响策略")
print("停更 7 种中，【不在策略池内】的:", out_pool)

with session_scope() as s:
    print("\n" + "=" * 92)
    print("停更品种在 daily_bar / akshare 源的可用性（补数可行性）")
    print("=" * 92)
    print(f"{'品种':<8}{'daily行数':>10}{'daily src':>34}{'最近日线':>12}")
    print("-" * 92)
    for sym in STALE:
        rows = s.execute(text("""
            SELECT src, count(*) n, max(trade_date) mx
            FROM l0_raw.daily_bar WHERE symbol=:s GROUP BY 1 ORDER BY n DESC
        """), {"s": sym}).fetchall()
        if not rows:
            print(f"{sym:<8}(daily_bar 无)")
            continue
        tot = sum(r._mapping["n"] for r in rows)
        srcs = ",".join(f"{r._mapping['src']}({r._mapping['n']})" for r in rows)
        latest = max(str(r._mapping["mx"]) for r in rows)
        print(f"{sym:<8}{tot:>10}{srcs:>34}{latest:>12}")

    print("\n" + "=" * 92)
    print("对照：策略池内的主力品种最近数据新鲜度")
    print("=" * 92)
    rows = s.execute(text("""
        SELECT symbol, max(trade_datetime)::date mx
        FROM l0_raw.hourly_bar
        WHERE symbol = ANY(:ss) GROUP BY 1 ORDER BY 1
    """, ), {"ss": main}).fetchall() if False else None

    rows = s.execute(text("""
        SELECT symbol, max(trade_datetime)::date mx
        FROM l0_raw.hourly_bar GROUP BY 1 ORDER BY 1
    """)).fetchall()
    fresh = {r._mapping["symbol"]: str(r._mapping["mx"]) for r in rows}
    from collections import Counter
    cnt = Counter(fresh.get(m, "无数据") for m in main)
    print("策略池 50 品种的小时线最近更新日期分布:")
    for d, c in sorted(cnt.items(), reverse=True)[:10]:
        print(f"   {d}: {c} 个品种")
    stale_in_pool = [(m, fresh.get(m)) for m in main
                     if fresh.get(m, "0000") < "2026-10-25"]
    print("\n策略池内【新鲜度落后】的品种:", stale_in_pool if stale_in_pool else "无")

    print("\n" + "=" * 92)
    print("kqm_1m 停摆影响：agg_1m 小时线的缺口")
    print("=" * 92)
    rows = s.execute(text("""
        SELECT src, max(trade_datetime)::date mx, count(DISTINCT symbol) n_sym
        FROM l0_raw.hourly_bar GROUP BY 1
    """)).fetchall()
    for r in rows:
        m = r._mapping
        print(f"   src={m['src']:<12} 最新={m['mx']}  品种数={m['n_sym']}")
