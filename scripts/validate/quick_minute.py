# -*- coding: utf-8 -*-
"""快速查 minute_bar 按品种的最后更新（小范围取样，避免全表扫描）。"""
from sqlalchemy import text
from app.core.db import session_scope

SYMS = ["BR888", "CJ888", "EC888", "LU888", "PF888", "PG888", "PK888",
        "RB888", "CU888", "AU888"]

with session_scope() as s:
    print("=" * 88)
    print("minute_bar 逐品种最后更新（停更 7 种 + 对照 3 种）")
    print("=" * 88)
    print(f"{'品种':<8}{'src':<14}{'行数':>10}{'最后更新':>14}")
    print("-" * 88)
    for sym in SYMS:
        rows = s.execute(text("""
            SELECT src, count(*) n, max(ts)::date mx
            FROM l0_raw.minute_bar WHERE symbol = :s GROUP BY 1 ORDER BY mx DESC
        """), {"s": sym}).fetchall()
        if not rows:
            print(f"{sym:<8}(无数据)")
            continue
        for r in rows:
            m = r._mapping
            print(f"{sym:<8}{str(m['src']):<14}{m['n']:>10}{str(m['mx']):>14}")
        print("-" * 88)
