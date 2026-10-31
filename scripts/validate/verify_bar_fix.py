# -*- coding: utf-8 -*-
"""验证 bar_60m / bar_30m 终版重建结果是否正确（重点：夜盘跨午夜不偏 1 分钟）。

用法:
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc venv/bin/python /tmp/verify_bar_fix.py
"""
from sqlalchemy import text

from app.core.db import session_scope


def sh(ts):
    return ts.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))


def main() -> int:
    with session_scope() as s:
        # 1) SC888 2026-10-30 夜盘（跨午夜，旧推理逻辑会偏 1 分钟：00:01 而非 00:00）
        rows = s.execute(text("""
            SELECT to_char(bucket AT TIME ZONE 'Asia/Shanghai', 'MM-DD HH24:MI') b
            FROM l1_mkt.bar_60m
            WHERE symbol='SC888'
              AND (bucket AT TIME ZONE 'Asia/Shanghai')::date IN ('2026-10-30','2026-10-31')
            ORDER BY bucket
        """)).fetchall()
        print("SC888 10-30~10-31 夜盘 60m bar 时刻:")
        print("  ", " ".join(r[0] for r in rows))
        bad = [r[0] for r in rows if r[0].endswith(":01")]
        print("  -> 含 ':01' 碎片(应=0):", bad if bad else "无")

        # 2) RB888 日盘是否恰好 6 根 10:00 11:15 14:15 15:00 22:00 23:00
        rows = s.execute(text("""
            SELECT to_char(bucket AT TIME ZONE 'Asia/Shanghai', 'HH24:MI') b
            FROM l1_mkt.bar_60m
            WHERE symbol='RB888'
              AND (bucket AT TIME ZONE 'Asia/Shanghai')::date = '2023-06-15'
            ORDER BY bucket
        """)).fetchall()
        print("\nRB888 2023-06-15 60m bar 时刻:")
        print("  ", " ".join(r[0] for r in rows))
        expect = ["10:00", "11:15", "14:15", "15:00", "22:00", "23:00"]
        print("  -> 与期望一致:", [r[0] for r in rows] == expect)

        # 3) 全库 60m 每日 bar 数分布
        print("\n=== 60m 每日 bar 数分布 ===")
        for r in s.execute(text("""
            SELECT n, count(*) days, round(count(*)*100.0/sum(count(*)) over(),2) pct
            FROM (SELECT symbol, (bucket AT TIME ZONE 'Asia/Shanghai')::date d, count(*) n
                  FROM l1_mkt.bar_60m GROUP BY 1,2) x GROUP BY n ORDER BY n
        """)).fetchall():
            print(f"   每天 {r[0]:>2} 根  天数={r[1]:>7,}  {r[2]:>5}%")

        # 4) 全库 30m 总行数 & 每日分布
        r60 = s.execute(text("SELECT count(*) FROM l1_mkt.bar_60m")).scalar()
        r30 = s.execute(text("SELECT count(*) FROM l1_mkt.bar_30m")).scalar()
        print(f"\n总行数: bar_60m={r60:,}  bar_30m={r30:,}")
        print("\n=== 30m 每日 bar 数分布 ===")
        for r in s.execute(text("""
            SELECT n, count(*) days, round(count(*)*100.0/sum(count(*)) over(),2) pct
            FROM (SELECT symbol, (bucket AT TIME ZONE 'Asia/Shanghai')::date d, count(*) n
                  FROM l1_mkt.bar_30m GROUP BY 1,2) x GROUP BY n ORDER BY n
        """)).fetchall():
            print(f"   每天 {r[0]:>2} 根  天数={r[1]:>7,}  {r[2]:>5}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
