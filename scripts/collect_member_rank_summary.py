# -*- coding: utf-8 -*-
"""member_position_rank_summary 历史回溯（PRD T9, P1）。

包装 app.ingest.member_rank_summary.run(start, end, vars_list)：按交易日逐日回填
「按合约前 20 汇总」表（注意：这是品种内各标的加总，不是逐会员明细，绝不能回填
member_position_rank）。回溯到 2018。
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
from app.ingest import member_rank_summary as mrs


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
    end = date.fromisoformat(args.end)
    products = [m.product for m in get_settings().main_contracts]
    if args.symbol:
        s = args.symbol.upper()
        products = [s[:-3] if s.endswith("888") else s]

    days = trading_days(start, end)
    print(f"[mpr_summary] {len(days)} trading days {start}~{end}, {len(products)} products", flush=True)
    fails = 0
    for d in days:
        try:
            res = mrs.run(d, d, products)
            if isinstance(res, dict) and res.get("rows") is None:
                fails += 1
            print(f"[mpr_summary] {d} -> {res}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[mpr_summary] FAIL {d}: {e}", flush=True)
        time.sleep(args.sleep)
    print(f"DONE days={len(days)} fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
