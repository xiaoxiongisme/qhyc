# -*- coding: utf-8 -*-
"""对比不同分钟源的开盘分钟序列，定位 bar 标签偏移 1 分钟的根因。"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope


def main() -> int:
    with session_scope() as s:
        q = text("""
            SELECT src,
                   to_char(min(ts) AT TIME ZONE 'Asia/Shanghai','HH24:MI') f,
                   to_char(max(ts) AT TIME ZONE 'Asia/Shanghai','HH24:MI') l,
                   count(*) n
            FROM l0_raw.minute_bar
            WHERE symbol = :sym AND (ts AT TIME ZONE 'Asia/Shanghai')::date = :d
              AND (ts AT TIME ZONE 'Asia/Shanghai')::time
                  BETWEEN TIME '08:50' AND TIME '15:10'
            GROUP BY src ORDER BY src
        """)
        cases = [("ZN888", "2025-06-03", "kqm_1m"),
                 ("ZN888", "2026-10-27", "akshare_1min"),
                 ("ZN888", "2026-09-24", "tqsdk_1min"),
                 ("SC888", "2026-10-27", "akshare_1min")]
        for sym, d, _ in cases:
            print(f"--- {sym} {d} 日盘(08:50-15:10) ---")
            for r in s.execute(q, {"sym": sym, "d": d}):
                print(f"   {r[0]:<13} first={r[1]}  last={r[2]}  n={r[3]}")

        print()
        for sym, d, src in cases:
            qq = text("""
                SELECT to_char(ts AT TIME ZONE 'Asia/Shanghai','HH24:MI')
                FROM l0_raw.minute_bar
                WHERE symbol = :sym AND src = :src
                  AND (ts AT TIME ZONE 'Asia/Shanghai')::date = :d
                  AND (ts AT TIME ZONE 'Asia/Shanghai')::time
                      BETWEEN TIME '08:55' AND TIME '09:06'
                ORDER BY ts
            """)
            mins = [r[0] for r in s.execute(qq, {"sym": sym, "src": src, "d": d})]
            print(f"{sym} {d} {src:<13} 开盘前几分钟 = {mins}")

            qq2 = text("""
                SELECT to_char(ts AT TIME ZONE 'Asia/Shanghai','HH24:MI')
                FROM l0_raw.minute_bar
                WHERE symbol = :sym AND src = :src
                  AND (ts AT TIME ZONE 'Asia/Shanghai')::date = :d
                  AND (ts AT TIME ZONE 'Asia/Shanghai')::time
                      BETWEEN TIME '14:54' AND TIME '15:05'
                ORDER BY ts
            """)
            mins2 = [r[0] for r in s.execute(qq2, {"sym": sym, "src": src, "d": d})]
            print(f"{'':>11}{'':>11} 收盘前后分钟 = {mins2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
