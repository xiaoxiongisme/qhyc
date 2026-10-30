# -*- coding: utf-8 -*-
"""查清各分钟源的 ts 时间戳约定（起点 vs 结束），解释 bar 标签 1 分钟偏移。

现象：kqm_1m 段切出来是 10:00 / 11:15（与同花顺一致），
而 akshare_1min / tqsdk_1min 段切出来是 10:01 / 11:16（整体晚 1 分钟）。
怀疑后者把分钟时间戳记为该分钟的「结束时刻」，前者记为「开始时刻」。
本脚本用每个源每天第一根/最后一根分钟的时刻差值来判定。
"""
from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import text
from zoneinfo import ZoneInfo

from app.core.db import session_scope

CST = ZoneInfo("Asia/Shanghai")


def main() -> int:
    with session_scope() as s:
        print("=== 各 src 每天首根/末根分钟时刻与分钟数（判定 ts 约定）===")
        rows = s.execute(text("""
            SELECT src, symbol, (ts AT TIME ZONE 'Asia/Shanghai')::date d,
                   to_char(min(ts) AT TIME ZONE 'Asia/Shanghai', 'HH24:MI') first_ts,
                   to_char(max(ts) AT TIME ZONE 'Asia/Shanghai', 'HH24:MI') last_ts,
                   count(*) n,
                   count(DISTINCT ts) n_distinct
            FROM l0_raw.minute_bar
            WHERE symbol = 'ZN888'
              AND (ts AT TIME ZONE 'Asia/Shanghai')::date IN
                  (DATE '2025-06-03', DATE '2026-09-24', DATE '2026-10-27')
            GROUP BY 1,2,3 ORDER BY 3,1
        """)).fetchall()
        for r in rows:
            print(f"  {r[2]}  {r[0]:<13} first={r[3]}  last={r[4]}  n={r[5]:>4}  distinct={r[6]:>4}")

        print("\n=== 同一分钟 (ts) 是否被多个 src 重复写入（重叠）===")
        rows = s.execute(text("""
            SELECT count(*) FROM (
                SELECT symbol, ts, count(DISTINCT src) k
                FROM l0_raw.minute_bar
                GROUP BY symbol, ts HAVING count(DISTINCT src) > 1
            ) x
        """)).fetchall()
        print(f"  跨 src 重复的 (symbol, ts) 数 = {rows[0][0]:,}")

        print("\n=== 各 src 的 ts 秒级成分（是否都是 :00 秒）===")
        rows = s.execute(text("""
            SELECT src, to_char(ts AT TIME ZONE 'Asia/Shanghai','SS') sec, count(*) n
            FROM l0_raw.minute_bar
            WHERE ts >= '2026-09-01'
            GROUP BY 1,2 ORDER BY 1,3 DESC LIMIT 12
        """)).fetchall()
        for r in rows:
            print(f"  {r[0]:<13} 秒={r[1]}  n={r[2]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
