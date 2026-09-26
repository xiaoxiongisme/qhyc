# -*- coding: utf-8 -*-
"""roll_yield 历史回溯（PRD T4）。

包装 app.ingest.roll_yield.run(date, vars_list)：按交易日逐日、按品种循环，回溯到 2018。
支持 --symbol 限定品种；失败容忍；退出码反映失败日数（A8）。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine
from app.ingest import roll_yield as ry


def trading_days(start: date, end: date) -> list[date]:
    eng = get_engine()
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT DISTINCT trade_date FROM daily_bar "
                "WHERE trade_date BETWEEN :s AND :e ORDER BY 1"
            ),
            {"s": start, "e": end},
        ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--symbol", help="单个主连代码如 RB888，按品种回溯")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.today() if args.end == "today" else date.fromisoformat(args.end)
    products = [m.product for m in get_settings().main_contracts]
    if args.symbol:
        s = args.symbol.upper()
        products = [s[:-3] if s.endswith("888") else s]

    days = trading_days(start, end)
    print(f"[roll_yield] {len(days)} trading days {start}~{end}, {len(products)} products", flush=True)
    fails = 0
    for d in days:
        try:
            res = ry.run(d, products)
            day_failed = any(isinstance(v, str) and "失败" in v for v in (res or {}).values())
            if day_failed:
                fails += 1
            print(f"[roll_yield] {d} -> {res}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[roll_yield] FAIL {d}: {e}", flush=True)
        time.sleep(args.sleep)
    print(f"DONE days={len(days)} fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
