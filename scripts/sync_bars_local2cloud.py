#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复云端 L1 bar_* 近期断档：本地已验证完整 -> 云端 upsert(ON CONFLICT DO NOTHING)。
只插云端缺失的 (symbol,bucket)，不动云端已有历史。非破坏、可逆。
根因：sp_build_l1_from_minute 用 pg_tables 探测 minute_bar(实际是 public 视图) -> 恒跳过。
本脚本为数据层应急修复，不依赖该存储过程。
"""
import psycopg2, datetime as _dt

PW = "qhyc_dev_pwd_2026"; DB = "futures"; USER = "futures"
CLOUD = dict(host="127.0.0.1", port=15432, user=USER, password=PW, dbname=DB)
LOCAL = dict(host="127.0.0.1", port=5432, user=USER, password=PW, dbname=DB)
TABLES = ["bar_15m", "bar_30m", "bar_60m"]  # bar_5m 已在本轮跑完
WIN_START = "2024-01-01"   # 只修近期窗口（云端断档在 2026 年起，留足余量）
BATCH = 50000


def conn(cfg):
    c = psycopg2.connect(**cfg); c.set_session(autocommit=False)
    c.cursor().execute("SET statement_timeout=180000"); return c


def cols_of(cur, schema, table):
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))
    return [r[0] for r in cur.fetchall()]


def main():
    cc = conn(CLOUD); cl = conn(LOCAL)
    cu = cc.cursor(); lu = cl.cursor()
    lu.execute("SET statement_timeout=180000")
    for table in TABLES:
        cols = cols_of(lu, "l1_mkt", table)
        colsql = ",".join(f'"{c}"' for c in cols)
        # 云端断档起点不明，按月分块从 WIN_START 拉本地数据 upsert
        start = _dt.date(2024, 1, 1)
        end = _dt.date(2027, 1, 1)
        done = 0
        m = start
        while m < end:
            nxt = (m.replace(day=28) + _dt.timedelta(days=5)).replace(day=1)
            lu.execute(
                f'DECLARE cur CURSOR FOR SELECT {colsql} FROM l1_mkt."{table}" '
                f"WHERE bucket >= %s AND bucket < %s ORDER BY bucket",
                (m.isoformat(), nxt.isoformat()))
            while True:
                lu.execute("FETCH %s FROM cur", (BATCH,))
                rows = lu.fetchall()
                if not rows:
                    break
                # 用 execute_values upsert(忽略冲突=只插缺失)
                from psycopg2.extras import execute_values
                try:
                    execute_values(cu,
                        f'INSERT INTO l1_mkt."{table}" ({colsql}) VALUES %s '
                        f'ON CONFLICT (symbol, bucket) DO NOTHING', rows,
                        page_size=2000)
                    cc.commit()
                    done += len(rows)
                except Exception as e:
                    cc.rollback()
                    print(f"  [ERR] {table} {m} batch: {e}")
                    raise
            lu.execute("CLOSE cur")
            # 进度（每月一块，打印累计）
            m = nxt
            print(f"  {table} 累计 upsert 尝试 {done:,} 行 (至 {m})", flush=True)
        # 验证云端该表 2026-08 现在有无数据
        cu.execute(f'SELECT count(*), count(DISTINCT symbol) FROM l1_mkt."{table}" '
                   "WHERE bucket >= '2026-08-01' AND bucket < '2026-09-01'")
        r, s = cu.fetchone()
        print(f"[OK] {table}: 云端 2026-08 现 rows={r:,} symbols={s}  (本表累计处理 {done:,})", flush=True)
    cc.close(); cl.close()


if __name__ == "__main__":
    main()
