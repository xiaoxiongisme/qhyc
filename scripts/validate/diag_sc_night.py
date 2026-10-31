# -*- coding: utf-8 -*-
"""诊断 SC888 2026-10-30 夜盘（跨午夜）分钟源与 ts 约定。"""
from sqlalchemy import text

from app.core.db import session_scope

_SH = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


def main() -> int:
    with session_scope() as s:
        # 该夜盘跨午夜：21:00(10-30) ~ 02:30(10-31)
        rows = s.execute(text("""
            SELECT to_char(ts AT TIME ZONE 'Asia/Shanghai','MM-DD HH24:MI') ts_sh,
                   src
            FROM l0_raw.minute_bar
            WHERE symbol='SC888'
              AND ts AT TIME ZONE 'Asia/Shanghai' >= '2026-10-30 21:00'::timestamp
              AND ts AT TIME ZONE 'Asia/Shanghai' <  '2026-10-31 02:31'::timestamp
            ORDER BY ts
        """)).fetchall()
        print(f"SC888 10-30 21:00 ~ 10-31 02:30 分钟数 = {len(rows)}")
        # 按 src 分组，打印每种 src 的前 3 和后 3 个 ts
        by_src: dict[str, list] = {}
        for ts_sh, src in rows:
            by_src.setdefault(src, []).append(ts_sh)
        for src, lst in by_src.items():
            print(f"\n  src={src}: 共 {len(lst)} 根")
            print(f"    首3: {lst[:3]}")
            print(f"    尾3: {lst[-3:]}")

        # 看 bar_60m 实际产出的该段
        print("\n=== bar_60m 该段（终版）===")
        for r in s.execute(text("""
            SELECT to_char(bucket AT TIME ZONE 'Asia/Shanghai','MM-DD HH24:MI') b
            FROM l1_mkt.bar_60m
            WHERE symbol='SC888'
              AND bucket AT TIME ZONE 'Asia/Shanghai' >= '2026-10-30 21:00'::timestamp
              AND bucket AT TIME ZONE 'Asia/Shanghai' <  '2026-10-31 02:31'::timestamp
            ORDER BY bucket
        """)).fetchall():
            print(f"    {r[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
