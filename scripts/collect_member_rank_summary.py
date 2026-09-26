# -*- coding: utf-8 -*-
"""会员持仓排名汇总（member_position_rank_summary）采集。

为什么不用 akshare 的 get_rank_sum_daily
----------------------------------------
akshare 的 get_rank_sum_daily 内部下载交易所 Excel/zip，在 headless 容器里
抛 BadZipFile（PRD §7 已知限制），导致 member_position_rank_summary 始终为空。

本脚本的更稳健方案：直接由 **已采集的逐会员明细表 member_position_rank**
（rank 1~20 的 long_pos/short_pos/long_chg/short_chg/vol_pos）按品种聚合出
top5 / top10 / top15 / top20 的六类指标，幂等 upsert 进 member_position_rank_summary。

* DCE 的逐会员明细已由 scrapling DCE 爬虫提供（scrapling:dce_memberDealPosi）；
* CZCE/SHFE/GFEX/CFFEX 的逐会员明细由 akshare 提供；
  两者口径一致，聚合结果即「品种持仓排名汇总」。

落库口径（member_position_rank_summary，PK: report_date+symbol+version）：
- symbol = 品种代码 + '888'（如 A2611 → A888），与既有约定一致
- variety = 品种代码（去数字后缀）
- 六类指标 × 4 档：vol_topN / long_open_interest_topN / short_open_interest_topN
                      及其 _chg 版本；vol_chg_topN 源表无字段 → NULL
- src = scrapling:member_rank_summary

DB 连接：优先读环境变量 PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD，
默认 localhost:5432（本地）；服务器端设 PGHOST=timescaledb 即可复用。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date as _date, timedelta as _td

import psycopg2  # noqa: E402
from psycopg2.extras import execute_batch  # noqa: E402

SRC = "scrapling:member_rank_summary"
VERSION = "v1.0"

PG = dict(
    host=os.environ.get("PGHOST", "localhost"),
    port=int(os.environ.get("PGPORT", "5432")),
    dbname=os.environ.get("PGDATABASE", "futures"),
    user=os.environ.get("PGUSER", "futures"),
    password=os.environ.get("PGPASSWORD", "qhyc_dev_pwd_2026"),
)

# 24 个数值列：4 档(5/10/15/20) × 6 指标
_RANKS = [5, 10, 15, 20]
# (列前缀, 源字段 or None)
_METRICS = [
    ("vol_top", "vol_pos"),
    ("vol_chg_top", None),                        # 源表无 vol_chg → NULL
    ("long_open_interest_top", "long_pos"),
    ("long_open_interest_chg_top", "long_chg"),
    ("short_open_interest_top", "short_pos"),
    ("short_open_interest_chg_top", "short_chg"),
]

NUM_COLS: list[str] = []
for _n in _RANKS:
    for _pre, _src in _METRICS:
        NUM_COLS.append(f"{_pre}{_n}")


def _select_exprs() -> str:
    parts = []
    for _n in _RANKS:
        for _pre, _src in _METRICS:
            if _src is None:
                parts.append("NULL")
            else:
                parts.append(f"SUM(CASE WHEN rank<={_n} THEN {_src} END)")
    return ", ".join(parts)


def aggregate_day(d: _date, exchange: str | None = None) -> list[dict]:
    conn = psycopg2.connect(connect_timeout=10, **PG)
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT trade_date,\n"
                "       UPPER(REGEXP_REPLACE(symbol, '\\d', '', 'g')) || '888' AS symbol,\n"
                "       UPPER(REGEXP_REPLACE(symbol, '\\d', '', 'g')) AS variety,\n"
                f"       {_select_exprs()}\n"
                "FROM member_position_rank\n"
                "WHERE trade_date = %s\n"
                "GROUP BY trade_date, 2, 3"
            )
            params: list = [d]
            if exchange:
                sql += " AND exchange=%s"
                params.append(exchange)
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d0, symbol, variety = r[0], r[1], r[2]
        vals = r[3:]
        row = {"report_date": d0, "symbol": symbol, "variety": variety,
               "src": SRC, "version": VERSION}
        for col, v in zip(NUM_COLS, vals):
            row[col] = v
        out.append(row)
    return out


def aggregate_range(s: _date, e: _date, exchange: str | None = None) -> list[dict]:
    """整段区间单条 SQL 聚合（一次 GROUP BY 覆盖全部交易日），用于全量回填。"""
    conn = psycopg2.connect(connect_timeout=10, **PG)
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT trade_date,\n"
                "       UPPER(REGEXP_REPLACE(symbol, '\\d', '', 'g')) || '888' AS symbol,\n"
                "       UPPER(REGEXP_REPLACE(symbol, '\\d', '', 'g')) AS variety,\n"
                f"       {_select_exprs()}\n"
                "FROM member_position_rank\n"
                "WHERE trade_date BETWEEN %s AND %s"
            )
            params: list = [s, e]
            if exchange:
                sql += " AND exchange=%s"
                params.append(exchange)
            sql += "\nGROUP BY trade_date, 2, 3"
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d0, symbol, variety = r[0], r[1], r[2]
        vals = r[3:]
        row = {"report_date": d0, "symbol": symbol, "variety": variety,
               "src": SRC, "version": VERSION}
        for col, v in zip(NUM_COLS, vals):
            row[col] = v
        out.append(row)
    return out


def upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ["report_date", "symbol", "variety"] + NUM_COLS + ["src", "version"]
    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in NUM_COLS)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = (
        f"INSERT INTO member_position_rank_summary ({', '.join(cols)})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT (report_date, symbol, version) DO UPDATE SET {set_clause}"
    )
    data = [tuple(row[c] for c in cols) for row in rows]
    conn = psycopg2.connect(connect_timeout=10, **PG)
    try:
        with conn, conn.cursor() as cur:
            execute_batch(cur, sql, data, page_size=500)
        return len(data)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="会员持仓排名汇总采集（聚合 member_position_rank）")
    ap.add_argument("--date", help="单日 YYYY-MM-DD")
    ap.add_argument("--start", help="开始 YYYY-MM-DD")
    ap.add_argument("--end", help="结束 YYYY-MM-DD（含）")
    ap.add_argument("--exchange", help="按交易所过滤（可选：DCE/CZCE/SHFE/GFEX/CFFEX）")
    args = ap.parse_args()

    if args.date:
        d = _date.fromisoformat(args.date)
        n = upsert(aggregate_day(d, args.exchange))
        print(f"[member_rank_summary] {d} 入库 {n} 行")
        return 0

    s = _date.fromisoformat(args.start) if args.start else _date.today() - _td(days=30)
    e = _date.fromisoformat(args.end) if args.end else _date.today()
    n = upsert(aggregate_range(s, e, args.exchange))
    print(f"[member_rank_summary] 区间 {s}~{e} 合计入库 {n} 行")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
