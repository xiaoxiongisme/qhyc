# -*- coding: utf-8 -*-
"""本地库从云端同步基本面表（整改优先级 P2 · 附录 A2/A3）。

背景（测试报告口径修正后）：云端 member_position_rank 已达 2015 起全量、roll_yield
有 884 行；**本地 roll_yield = 0 行**。缺口性质是「本地未同步」而非「从未采集」，
因此无需重跑历史 API，直接云 → 地搬运即可。

容器运行视角（Windows Docker）：
  - 云端 = host.docker.internal:15432（宿主 SSH 隧道）
  - 本地 = timescaledb:5432（docker 服务名）

用法：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/sync_local_fundamentals.py --tables roll_yield
    ... --tables roll_yield,member_position_rank   # 大表，谨慎
    ... --dry-run                                   # 只报数不写
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2

DEFAULT_TABLES = ["roll_yield"]


def src_conn_args(a):
    return dict(
        host=os.getenv("CLOUD_PG_HOST", a.src_host or "host.docker.internal"),
        port=int(os.getenv("CLOUD_PG_PORT", a.src_port or 15432)),
        dbname=os.getenv("CLOUD_PG_DB", a.src_db or "futures"),
        user=os.getenv("CLOUD_PG_USER", a.src_user or "futures"),
        password=os.getenv("CLOUD_PG_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")),
    )


def dst_conn_args(a):
    return dict(
        host=os.getenv("SYNC_DST_HOST", a.dst_host or "timescaledb"),
        port=int(os.getenv("SYNC_DST_PORT", a.dst_port or 5432)),
        dbname=os.getenv("POSTGRES_DB", "futures"),
        user=os.getenv("POSTGRES_USER", "futures"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


def sync_table(sc, dc, table: str, dry: bool = False) -> dict:
    with sc.cursor() as cur:
        cur.execute('SELECT count(*) FROM "{0}"'.format(table))
        n_src = cur.fetchone()[0]
    with dc.cursor() as cur:
        cur.execute('SELECT count(*) FROM "{0}"'.format(table))
        n_dst = cur.fetchone()[0]
    info = {"table": table, "src_rows": n_src, "dst_rows_before": n_dst}
    if n_src == 0:
        info["status"] = "aborted: 源为空（不误清空本地）"
        return info
    if dry:
        info["status"] = "dry-run"
        return info
    # 流式搬运：源 COPY TO STDOUT → 目标 TRUNCATE + COPY FROM STDIN
    with sc.cursor() as s_cur, dc.cursor() as d_cur, \
            open("/tmp/_sync_{0}.dat".format(table), "wb") as tmp:
        s_cur.copy_expert('COPY (SELECT * FROM "{0}") TO STDOUT'.format(table), tmp)
        tmp.flush()
        d_cur.execute('TRUNCATE TABLE "{0}"'.format(table))
        with open("/tmp/_sync_{0}.dat".format(table), "rb") as fh:
            d_cur.copy_expert('COPY "{0}" FROM STDIN'.format(table), fh)
    dc.commit()
    with dc.cursor() as cur:
        cur.execute('SELECT count(*) FROM "{0}"'.format(table))
        info["dst_rows_after"] = cur.fetchone()[0]
    info["status"] = "ok"
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--src-host"), ap.add_argument("--src-port", type=int)
    ap.add_argument("--src-db"), ap.add_argument("--src-user")
    ap.add_argument("--dst-host"), ap.add_argument("--dst-port", type=int)
    a = ap.parse_args()

    tables = [t.strip() for t in a.tables.split(",") if t.strip()]
    print("源(云端)：{0}:{1}".format(
        os.getenv("CLOUD_PG_HOST", a.src_host or "host.docker.internal"),
        os.getenv("CLOUD_PG_PORT", a.src_port or 15432)))
    print("目标(本地)：{0}:{1}".format(
        os.getenv("SYNC_DST_HOST", a.dst_host or "timescaledb"),
        os.getenv("SYNC_DST_PORT", a.dst_port or 5432)))
    print("表：{0}{1}".format(",".join(tables), "  [dry-run]" if a.dry_run else ""))

    try:
        sc = psycopg2.connect(**src_conn_args(a))
    except Exception as e:
        print("❌ 无法连接云端（隧道 15432 是否存活？）：{0}".format(str(e)[:200]))
        return 2
    try:
        dc = psycopg2.connect(**dst_conn_args(a))
    except Exception as e:
        print("❌ 无法连接本地库：{0}".format(str(e)[:200]))
        return 2

    ok = True
    try:
        for t in tables:
            try:
                info = sync_table(sc, dc, t, a.dry_run)
            except Exception as e:
                info = {"table": t, "status": "error: {0}".format(str(e)[:180])}
                ok = False
            print("  {0}".format(info))
    finally:
        sc.close()
        dc.close()
    print("\n同步完成：{0}".format("OK" if ok else "存在错误"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
