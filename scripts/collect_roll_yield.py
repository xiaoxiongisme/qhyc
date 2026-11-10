# -*- coding: utf-8 -*-
"""roll_yield 历史回溯（PRD T4）。

包装 app.ingest.roll_yield.run(date, vars_list)：按交易日逐日、按品种循环，回溯到 2018。
支持 --symbol 限定品种；失败容忍；退出码反映失败日数（A8）。

★ 2026-11-10（CL-20261110-15 E1）：新增 ``--require-rows``。
事故：11-09 作业 ``scheduled_task_run.status=success``，但 ``roll_yield`` 当日
**0 行**——上游 contract_daily 晚到，``app.ingest.roll_yield.run`` 对每个品种
``save_rows([]) -> 0`` 返回 ``{var: 0}``，旧判据只认字符串里的「失败」，
于是全 0 也算 ``fails=0`` → 退出码 0 → ``_require_subprocess_success`` 通过 →
流水假绿。数据次日靠 ``max(report_date)+1`` 续跑补回，但**当天没人知道它没写**。
开启本开关后，「整个交易日 0 行」计为失败，退出码非 0，让延迟可见；
手工历史回溯（2018 起大量无数据年份）不传该开关，行为不变。
"""
from __future__ import annotations

import argparse
import re
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


def day_written_rows(res: dict) -> int:
    """把 run() 的 {var: 行数/错误} 汇总成实际写入行数。

    ``run`` 成功时值是 int；db_fallback 分支值是 ``'47(db_fallback)'``；
    双失败时是 ``'失败: ...'``。只把前两种的正数计入，字符串错误信息算 0。
    """
    total = 0
    for v in (res or {}).values():
        if isinstance(v, (int, float)):
            total += int(v)
        elif isinstance(v, str):
            m = re.match(r"^(\d+)", v.strip())
            if m:
                total += int(m.group(1))
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--symbol", help="单个主连代码如 RB888，按品种回溯")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--require-rows", action="store_true",
                    help="整个交易日 0 行视为失败（供每日调度使用，防静默成功）")
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
    empty_days: list[str] = []
    for d in days:
        try:
            res = ry.run(d, products)
            day_failed = any(isinstance(v, str) and "失败" in v for v in (res or {}).values())
            written = day_written_rows(res)
            if day_failed:
                fails += 1
            if args.require_rows and written == 0:
                # 0 行 != 成功：上游晚到时必须让退出码非 0，否则又是假绿。
                empty_days.append(str(d))
                if not day_failed:
                    fails += 1
                print(f"[roll_yield] EMPTY_DAY {d} rows=0 "
                      f"(上游 contract_daily 未就绪，下一轮 max(report_date)+1 自动续跑)",
                      flush=True)
            print(f"[roll_yield] {d} -> rows={written} {res}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[roll_yield] FAIL {d}: {e}", flush=True)
        time.sleep(args.sleep)
    if empty_days:
        print(f"[roll_yield] EMPTY_DAYS={','.join(empty_days)} "
              f"共 {len(empty_days)} 个交易日零写入，退出码非 0（CL-20261110-15）",
              flush=True)
    print(f"DONE days={len(days)} fails={fails} empty_days={len(empty_days)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
