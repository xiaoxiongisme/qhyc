# -*- coding: utf-8 -*-
"""体检：小时线在历史早段的稀疏度，解释对照组 A/B 段 n/a 的成因。

若 hourly_bar(src=akshare) 在 2016-2018 样本极少，则"小时线 vs 日线"的
历史段对比不成立（对照组有偏），只能比较两者样本都充足的近期。
"""
import sys

from datetime import date
from sqlalchemy import text
from app.core.db import session_scope


def main() -> int:
    sym = "CU888"
    bands = [(2015, 2018), (2016, 2018), (2019, 2021), (2022, 2024), (2025, 2026)]
    with session_scope() as s:
        print("=== hourly_bar(src=akshare) CU888 逐年/分段密度 ===")
        rows = s.execute(text(
            "SELECT extract(year FROM trade_datetime)::int AS y, count(*) "
            "FROM hourly_bar WHERE symbol=:s AND src='akshare' "
            "GROUP BY 1 ORDER BY 1"
        ), {"s": sym}).fetchall()
        for y, c in rows:
            print(f"  {y}: {c} 根")
        print()
        print("=== daily_bar(src=akshare) CU888 逐年密度（对照）===")
        rows = s.execute(text(
            "SELECT extract(year FROM trade_date)::int AS y, count(*) "
            "FROM l0_raw.daily_bar WHERE symbol=:s AND src='akshare' "
            "GROUP BY 1 ORDER BY 1"
        ), {"s": sym}).fetchall()
        for y, c in rows:
            print(f"  {y}: {c} 根")
        print()
        print("=== 全品种小时线各段可用品种数（>=160根才算可回测）===")
        for lo, hi in bands:
            n = s.execute(text(
                "SELECT count(*) FROM ("
                "  SELECT symbol FROM hourly_bar "
                "  WHERE src='akshare' AND trade_datetime "
                "        BETWEEN :a AND :b "
                "  GROUP BY symbol HAVING count(*) >= 160) t"
            ), {"a": date(lo, 1, 1), "b": date(hi, 12, 31)}).scalar()
            print(f"  {lo}-{hi}: {n} 个品种具备 >=160 根")
    return 0


if __name__ == "__main__":
    sys.exit(main())
