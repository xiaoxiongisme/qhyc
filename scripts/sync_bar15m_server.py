# -*- coding: utf-8 -*-
"""把本地 bar_15m（及可选 bar_30m/bar_60m）同步到云端服务器。

前提：已通过 ssh -L 把云端 Postgres 5432 映射到本机某端口，例如：
    ssh -L 15432:timescaledb:5432 <user>@<cloud-host>
本脚本用纯 psycopg2 双连接（源=本地，目标=隧道端口）做分块 COPY，
目标端若不存在对应超表则自动建表 + create_hypertable。

环境变量（均有默认值）：
  SRC_PGHOST/PORT/DB/USER/PASSWORD   源库（本地），默认 localhost:5432/futures/futures/<无口令默认>
  DST_PGHOST/PORT/DB/USER/PASSWORD   目标库（云端隧道），默认 localhost:15432/futures/futures/<无口令默认>
  SYNC_TABLES                       逗号分隔表名，默认 bar_15m
  CHUNK_DAYS                        按时间分块天数，默认 60
用法：
  python scripts/sync_bar15m_server.py                # 仅 bar_15m
  python scripts/sync_bar15m_server.py --tables bar_15m,bar_30m,bar_60m
  python scripts/sync_bar15m_server.py --dry-run     # 只打印计划不写库
"""
import os
from __future__ import annotations
import os, sys, argparse, io
from datetime import timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psycopg2

DDL = {
    "bar_15m": """
CREATE TABLE IF NOT EXISTS bar_15m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric);
SELECT create_hypertable('bar_15m','bucket', if_not_exists=>true, chunk_time_interval=>interval '30 days');
""",
    "bar_30m": """
CREATE TABLE IF NOT EXISTS bar_30m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric);
SELECT create_hypertable('bar_30m','bucket', if_not_exists=>true, chunk_time_interval=>interval '60 days');
""",
    "bar_60m": """
CREATE TABLE IF NOT EXISTS bar_60m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric);
SELECT create_hypertable('bar_60m','bucket', if_not_exists=>true, chunk_time_interval=>interval '90 days');
""",
}
COLS = "symbol,bucket,open,high,low,close,volume,amount,open_interest"


def conn_from(prefix):
    return psycopg2.connect(
        host=os.environ.get(f"{prefix}_PGHOST", "localhost"),
        port=int(os.environ.get(f"{prefix}_PGPORT", "5432" if prefix == "SRC" else "15432")),
        dbname=os.environ.get(f"{prefix}_PGDATABASE", "futures"),
        user=os.environ.get(f"{prefix}_PGUSER", "futures"),
        password=os.environ[f"{prefix}_PGPASSWORD"],
        connect_timeout=30,
    )


def copy_table(src, dst, table, chunk_days, dry_run):
    cur = src.cursor()
    cur.execute(f'SELECT MIN(bucket), MAX(bucket), COUNT(*) FROM "{table}"')
    lo, hi, n = cur.fetchone()
    print(f"[{table}] 源行数={n:,}  范围={lo} ~ {hi}")
    if dry_run:
        return
    # 建表 + 超表
    dcur = dst.cursor()
    dcur.execute(DDL[table])
    dst.commit()
    # 清空目标（幂等重导）
    dcur.execute(f'TRUNCATE "{table}"')
    dst.commit()

    start = lo
    total = 0
    while start <= hi:
        end = start + timedelta(days=chunk_days)
        buf = io.StringIO()
        cur.execute(
            f'COPY (SELECT {COLS} FROM "{table}" WHERE bucket >= %s AND bucket < %s '
            f'ORDER BY bucket, symbol) TO STDOUT WITH (FORMAT CSV)',
            (start, end),
        )
        # copy_expert 的 stdout 写入 buf
        src_cursor_copy = src.cursor()
        src_cursor_copy.copy_expert(
            f'COPY (SELECT {COLS} FROM "{table}" WHERE bucket >= %s AND bucket < %s '
            f'ORDER BY bucket, symbol) TO STDOUT WITH (FORMAT CSV)',
            buf, (start, end),
        )
        buf.seek(0)
        dcur.copy_expert(f'COPY "{table}" ({COLS}) FROM STDIN WITH (FORMAT CSV)', buf)
        dst.commit()
        copied = buf.getvalue().count("\n")
        total += copied
        print(f"  chunk {start.date()} ~ {end.date()}: +{copied:,} 累计 {total:,}")
        start = end
    print(f"[{table}] 完成，写入 {total:,} 行")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default=os.environ.get("SYNC_TABLES", "bar_15m"))
    ap.add_argument("--chunk-days", type=int, default=int(os.environ.get("CHUNK_DAYS", "60")))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tables = [t.strip() for t in a.tables.split(",") if t.strip()]
    for t in tables:
        if t not in DDL:
            raise SystemExit(f"未知表 {t}，可选：{', '.join(DDL)}")

    src = conn_from("SRC")
    dst = conn_from("DST")
    try:
        for t in tables:
            copy_table(src, dst, t, a.chunk_days, a.dry_run)
    finally:
        src.close()
        dst.close()
    print("全部完成" if not a.dry_run else "dry-run 结束")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
