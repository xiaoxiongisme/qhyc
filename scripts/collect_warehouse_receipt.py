# -*- coding: utf-8 -*-
"""warehouse_receipt 历史回溯（PRD T5）。

包装 app.ingest.warehouse_receipt.run(date, exchanges)：按交易日逐日、四家交易所，
回溯到 2018。失败容忍；退出码反映失败日数（A8）。
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

from app.core.db import get_engine
from app.ingest import warehouse_receipt as wr


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
    ap.add_argument("--exchanges", default="CZCE,DCE,SHFE,GFEX")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]

    days = trading_days(start, end)
    print(f"[warehouse_receipt] {len(days)} trading days {start}~{end}, {exchanges}", flush=True)
    fails = 0
    for d in days:
        try:
            res = wr.run(d, exchanges=exchanges)
            day_failed = any(isinstance(v, str) and "失败" in v for v in (res or {}).values())
            if day_failed:
                fails += 1
            print(f"[warehouse_receipt] {d} -> {res}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[warehouse_receipt] FAIL {d}: {e}", flush=True)
        time.sleep(args.sleep)
    print(f"DONE days={len(days)} fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
