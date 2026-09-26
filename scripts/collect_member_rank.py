# -*- coding: utf-8 -*-
"""member_position_rank（逐会员）历史回溯（PRD T3）。

包装 app.ingest.rank_position.run(date)：按交易日逐日回溯到 2015，四家交易所
（SHFE/CZCE/GFEX 走 akshare 官方；DCE 走 Scrapling 通道，容器内若无依赖会自动降级跳过）。
支持 --symbol 限定其交易所；失败容忍；退出码反映失败日数（A8）。
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
from app.ingest import rank_position as rp


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
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--symbol", help="按品种回溯（映射其交易所）")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    exchanges = None
    if args.symbol:
        s = args.symbol.upper()
        prod = s[:-3] if s.endswith("888") else s
        ex = {m.product.upper(): m.exchange for m in get_settings().main_contracts}.get(prod)
        if ex:
            exchanges = [ex]

    days = trading_days(start, end)
    print(f"[member_rank] {len(days)} trading days {start}~{end}", flush=True)
    fails = 0
    for d in days:
        try:
            res = rp.run(d, exchanges=exchanges)
            day_failed = any(isinstance(v, str) and "失败" in v for v in (res or {}).values())
            if day_failed:
                fails += 1
            print(f"[member_rank] {d} -> {res}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[member_rank] FAIL {d}: {e}", flush=True)
        time.sleep(args.sleep)
    print(f"DONE days={len(days)} fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
