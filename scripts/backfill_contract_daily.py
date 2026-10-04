# -*- coding: utf-8 -*-
"""全量回补 contract_daily（历史 + 当前）。

枚举 dim_contract 中全部上市合约，按交易所取全历史日线并 upsert（append-only,
version='v1.0'，与现有采集器一致，同名(symbol,trade_date)后者覆盖）。

数据源：
  - DCE/SHFE/INE/GFEX  -> akshare sina（futures_zh_daily_sina，取全历史）
  - CZCE              -> akshare sina（futures_zh_daily_sina，取全历史；与 DCE/SHFE/INE/GFEX 同路径）
  - CFFEX             -> 跳过（金融期货 carry 弱）

并发与限频：sina 用 ThreadPoolExecutor(max_workers=4) + 每次 0.1s 限频。
失败计入 errors，不中断。（C9 去天勤：原 CZCE 走 天勤 全生命日线，现统一 akshare sina）

用法（容器内）：
  python scripts/backfill_contract_daily.py            # 全量
  python scripts/backfill_contract_daily.py --only RB,CU,AG   # 调试指定品种
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta as td
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import session_scope
from app.core import symbol_code as SC
from app.ingest.contract_bars import fetch_sina_contract_daily
from app.models import ContractDaily

VERSION = "v1.0"
SINA_EXCHANGES = {"DCE", "SHFE", "INE", "GFEX", "CZCE"}


def upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(ContractDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "trade_date", "version"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "settle": stmt.excluded.settle,
            "volume": stmt.excluded.volume,
            "oi": stmt.excluded.oi,
            "src": stmt.excluded.src,
            "product": stmt.excluded.product,
            "exchange": stmt.excluded.exchange,
        },
    )
    with session_scope() as s:
        s.execute(stmt)
        s.commit()
    return len(rows)


def already_backfilled(s, code: str) -> bool:
    """重跑优化：若已有较完整历史则跳过（首跑全量不会命中）。"""
    row = s.execute(
        text(
            "SELECT count(*), max(trade_date) FROM contract_daily "
            "WHERE symbol=:c AND version=:v"
        ),
        {"c": code, "v": VERSION},
    ).fetchone()
    cnt, mx = (row[0] or 0), row[1]
    if cnt >= 200 and mx and (date.today() - mx).days <= 5:
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="逗号分隔品种(如 RB,CU)仅处理这些")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    with session_scope() as s:
        contracts = s.execute(
            text("SELECT contract_code, exchange FROM dim_contract")
        ).fetchall()
    print(f"[backfill] dim_contract 合约数={len(contracts)}", flush=True)

    if a.only:
        want = {w.strip().upper() for w in a.only.split(",") if w.strip()}
        contracts = [c for c in contracts if c[0][: len(next(iter(want)))] in want
                     or any(c[0].startswith(w) for w in want)]
        # 简化：按品种前缀过滤
        contracts = [c for c in contracts if any(c[0].startswith(w) for w in want)]
        print(f"[backfill] --only 过滤后={len(contracts)}", flush=True)

    sina_jobs = []
    with session_scope() as s:
        for code, ex in contracts:
            if ex in SINA_EXCHANGES:
                sina_jobs.append((code, ex))
            # CFFEX 跳过
    print(f"[backfill] sina={len(sina_jobs)}", flush=True)

    errors = []
    total_rows = 0

    # ---- SINA 并发 ----
    def sina_task(job):
        code, ex = job
        try:
            rows = fetch_sina_contract_daily(code, days=36500, exchange=ex)
            if rows:
                for r in rows:
                    r["version"] = VERSION
                    r["product"] = code[:-4]
                    r["exchange"] = ex
            n = upsert(rows)
            time.sleep(0.1)
            return (code, n, None)
        except Exception as e:  # noqa: BLE001
            return (code, 0, str(e)[:120])

    if sina_jobs:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = [ex.submit(sina_task, j) for j in sina_jobs]
            done = 0
            for fut in as_completed(futs):
                code, n, err = fut.result()
                done += 1
                total_rows += n
                if err:
                    errors.append((code, err))
                if done % 100 == 0:
                    print(f"[backfill] sina 进度 {done}/{len(sina_jobs)} rows={total_rows}", flush=True)

    print(f"[backfill] 完成：写入行数={total_rows} 错误={len(errors)}", flush=True)
    if errors:
        print("[backfill] 错误样本(前20):", flush=True)
        for c, e in errors[:20]:
            print(f"   {c}: {e}", flush=True)


if __name__ == "__main__":
    main()
