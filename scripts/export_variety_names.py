# -*- coding: utf-8 -*-
"""从库内 dim_variety 导出「品种中文名 → 品种码/乘数」供外部解析脚本消费。

为什么需要
----
``scripts/build_cost_dict.py`` 要把交易所费率表里的**中文品种名**映射到品种码
（螺纹钢→RB、白银→AG、沪深300指数→IF…）。若把这些别名写死在解析脚本里，
就是又一份「代码内的字典」，与「字典表为唯一真源」的目标相悖。

故别名的真源放在 ``dim_variety.variety_name``：新增品种入库后映射自动生效，
解析脚本无需改动。

用法（云端容器内）：
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/export_variety_names.py            # 写到 /app/db/seed/
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/export_variety_names.py -o /tmp/x.csv
"""
from __future__ import annotations

import argparse
import csv
import os

from sqlalchemy import text

from app.core.db import session_scope

DEFAULT_OUT = os.path.join("db", "seed", "dim_variety_names.csv")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    with session_scope() as s:
        rows = s.execute(text(
            "SELECT variety_code, variety_name, exchange, multiplier "
            "FROM dim_variety "
            "WHERE is_active AND variety_name IS NOT NULL AND variety_name <> '' "
            "  AND variety_code !~ '888$' "
            "ORDER BY variety_code")).fetchall()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["variety_code", "variety_name", "exchange", "multiplier"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], "" if r[3] is None else r[3]])

    print(f"已导出 {len(rows)} 行 → {args.out}")
    if not rows:
        print("⚠ dim_variety 无可用中文名；请先跑 seed_variety_specs.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
