# -*- coding: utf-8 -*-
"""把本地基本面表同步到云端（经 SSH 隧道，默认本地 15432 端口映射云端 5432）。

幂等：单事务内 TRUNCATE + COPY。防护：本地某表为空时跳过，绝不误清空云端。
用法：
    python scripts/sync_fundamentals_server.py
    SYNC_TABLES=warehouse_receipt,member_position_rank_summary DST_PGPORT=15432 python scripts/sync_fundamentals_server.py
"""
from __future__ import annotations
import os, sys, io
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psycopg2
from factor_ic_scan import load_creds

TABLES = os.environ.get(
    "SYNC_TABLES", "warehouse_receipt,member_position_rank_summary"
).split(",")


def cols_of(conn, t):
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (t,),
    )
    return [r[0] for r in cur.fetchall()]


def main():
    # 本地源库（默认 127.0.0.1:5432）
    src = psycopg2.connect(**load_creds())
    # 云端目标库（隧道端口）
    os.environ["POSTGRES_PORT"] = os.environ.get("DST_PGPORT", "15432")
    try:
        dst = psycopg2.connect(**load_creds())
    except Exception as e:  # noqa: BLE001
        print(f"[error] 无法连接云端（隧道端口 {os.environ['POSTGRES_PORT']}）：{e}")
        return

    try:
        for t in TABLES:
            t = t.strip()
            scur = src.cursor()
            scur.execute(f'SELECT count(*) FROM "{t}"')
            n_src = scur.fetchone()[0]
            print(f"[src] {t} = {n_src:,} 行")
            if n_src == 0:
                print(f"  -> 本地为空，跳过（不误清空云端）")
                continue
            cols = cols_of(src, t)
            # 云端列比对，防止结构漂移
            dcols = cols_of(dst, t)
            if cols != dcols:
                print(f"  -> 列不一致（本地 {cols} / 云端 {dcols}），中止该表以避免错位")
                continue
            dcur = dst.cursor()
            dcur.execute(f'TRUNCATE "{t}"')
            buf = io.StringIO()
            scur.copy_expert(
                f'COPY (SELECT {",".join(cols)} FROM "{t}") TO STDOUT', buf
            )
            buf.seek(0)
            dcur.copy_expert(
                f'COPY "{t}" ({",".join(cols)}) FROM STDIN', buf
            )
            dcur.execute(f'SELECT count(*) FROM "{t}"')
            n_dst = dcur.fetchone()[0]
            dst.commit()
            print(f"[dst] {t} = {n_dst:,} 行（已同步）")
    finally:
        dst.close()
        src.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
