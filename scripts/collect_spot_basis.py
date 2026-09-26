# -*- coding: utf-8 -*-
"""spot_basis 历史回溯（PRD T2）。

包装 app.ingest.spot_basis.collect_spot_basis：按年切片回溯到 2015-01-01，全品种。
支持 --symbol 按品种回溯；幂等（upsert）；单段失败不中断全局；退出码反映失败段数（A8）。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.db import session_scope
from app.ingest import spot_basis as sb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--symbol", help="单个主连代码如 RB888，按品种回溯")
    ap.add_argument("--chunk-days", type=int, default=400)
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    products = None
    if args.symbol:
        s = args.symbol.upper()
        products = [s[:-3] if s.endswith("888") else s]

    fails = 0
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=args.chunk_days), end)
        try:
            with session_scope() as s:
                res = sb.collect_spot_basis(s, cur, nxt, products=products)
            print(f"[spot_basis] {cur}~{nxt} -> {res.get('rows')} rows", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[spot_basis] FAIL {cur}~{nxt}: {e}", flush=True)
        cur = nxt + timedelta(days=1)
        time.sleep(args.sleep)
    print(f"DONE fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
