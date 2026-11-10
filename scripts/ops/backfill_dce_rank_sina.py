# -*- coding: utf-8 -*-
"""DCE 会员持仓排名缺口回补（CL-20261110-11）。

只补 ``l0_raw.member_position_rank`` 里 **DCE 当日缺失的合约**，不删不改官网已有行。
数据来自 ``app.ingest.dce_sina``（新浪按单合约查询），三道质量硬门见该模块。

用法（生产容器内）：
-------------------------------------------------------------------
# 只读预演（默认，绝不写库）
python scripts/ops/backfill_dce_rank_sina.py --date 2026-11-09

# 扫描区间内所有「品种覆盖 < 16」的交易日并逐日预演
python scripts/ops/backfill_dce_rank_sina.py --start 2025-11-10 --end 2026-11-10

# 实际写入（自动先建 l3_ref.bak_cl20261110_11 备份）
python scripts/ops/backfill_dce_rank_sina.py --start 2025-11-10 --end 2026-11-10 --apply

# 指定多日
python scripts/ops/backfill_dce_rank_sina.py --date 2026-11-09 --date 2026-10-19 --apply
-------------------------------------------------------------------

回滚（万一需要）：按 ``src = 'sina:futures_hold_pos_sina'`` 精确删除即可，
例如 ``DELETE FROM l0_raw.member_position_rank WHERE src = 'sina:futures_hold_pos_sina'
AND trade_date = '2026-11-09'``；完整原分区在备份表里。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

from sqlalchemy import text

from app.core.db import get_engine
from app.ingest.dce_sina import SRC_TAG, TABLE, backfill_missing

BACKUP_TABLE = "l3_ref.bak_cl20261110_11"


def _auto_days(start: _dt.date, end: _dt.date, min_varieties: int) -> list[_dt.date]:
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT trade_date FROM " + TABLE + " "
            "WHERE exchange='DCE' AND trade_date BETWEEN :s AND :e "
            "GROUP BY 1 "
            "HAVING count(DISTINCT upper(regexp_replace(symbol,'[0-9]+$',''))) < :mv "
            "ORDER BY 1"
        ), {"s": start, "e": end, "mv": min_varieties}).all()
    return [r[0] for r in rows]


def _ensure_backup(days: list[_dt.date], table: str) -> str:
    """一次性把目标日的 DCE 原分区备份进归档表（按日幂等）。"""
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {table} AS "
            f"SELECT * FROM {TABLE} WHERE false"))
    n = 0
    with eng.begin() as conn:
        for d in days:
            r = conn.execute(text(
                f"INSERT INTO {table} SELECT m.* FROM {TABLE} m "
                "WHERE m.exchange='DCE' AND m.trade_date=:d "
                f"AND NOT EXISTS (SELECT 1 FROM {table} b "
                "WHERE b.trade_date=:d AND b.exchange='DCE')"
            ), {"d": d})
            n += r.rowcount or 0
    return f"{table} 备份 {n} 行 / {len(days)} 个交易日"


def main() -> int:
    ap = argparse.ArgumentParser(description="DCE 会员持仓排名缺口回补")
    ap.add_argument("--date", action="append", default=[], help="指定交易日，可重复")
    ap.add_argument("--start", help="自动扫描起始日 YYYY-MM-DD")
    ap.add_argument("--end", help="自动扫描结束日 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只读预演）")
    ap.add_argument("--no-verify", action="store_true", help="跳过门1 同日对拍（不建议）")
    ap.add_argument("--window", type=int, default=7, help="合约候选窗口（自然日，默认 7）")
    ap.add_argument("--min-varieties", type=int, default=16,
                    help="自动扫描阈值：品种数低于此值视为塌陷日（默认 16）")
    ap.add_argument("--backup-table", default=BACKUP_TABLE)
    args = ap.parse_args()

    days = [_dt.date.fromisoformat(x) for x in args.date]
    if args.start or args.end:
        if not (args.start and args.end):
            ap.error("--start / --end 必须同时给出")
        days += _auto_days(_dt.date.fromisoformat(args.start),
                           _dt.date.fromisoformat(args.end), args.min_varieties)
    days = sorted(set(days))
    if not days:
        print("无目标交易日（可能区间内无塌陷日，或需显式 --date）")
        return 0

    print(f"模式={'APPLY 写库' if args.apply else 'DRY-RUN 只读'} | "
          f"目标 {len(days)} 个交易日: {', '.join(map(str, days))}", flush=True)
    if args.apply:
        print(_ensure_backup(days, args.backup_table), flush=True)
    else:
        print(f"（只读预演，不建备份；--apply 时会写 {args.backup_table}）", flush=True)

    summary = []
    for d in days:
        r = backfill_missing(d, window_days=args.window,
                             verify_existing=not args.no_verify, apply=args.apply)
        summary.append(r)
        print(json.dumps(r, ensure_ascii=False, default=str), flush=True)
        if not r["ok"]:
            print(f"  ⚠ {d} 拒写：{r['reason']}", flush=True)

    ok = sum(1 for r in summary if r["ok"])
    rows = sum(r["net_new"] for r in summary)
    print(f"=== 汇总 {ok}/{len(summary)} 日通过硬门，净新增 {rows} 行 "
          f"(src={SRC_TAG}) ===", flush=True)
    return 0 if ok == len(summary) else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
