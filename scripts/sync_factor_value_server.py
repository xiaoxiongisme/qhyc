# -*- coding: utf-8 -*-
"""将本地 factor_value 同步到云端（经 SSH 隧道，默认本地 15432 端口映射云端 5432）。

前置：已通过 ssh -L 把云端 Postgres 映射到本机端口（默认 15432），例如：
    ssh -L 15432:timescaledb:5432 <user>@<cloud-host>
（云端容器服务名为 timescaledb；若云端宿主直接暴露则用 localhost）

用法：
    python scripts/sync_factor_value_server.py
    DST_PGPORT=15432 python scripts/sync_factor_value_server.py   # 显式指定隧道端口

逻辑：云端 CREATE TABLE IF NOT EXISTS factor_value（同 DDL），单事务内 TRUNCATE + COPY，
保证幂等。防护：本地 factor_value 为空时中止，绝不误清空云端。
"""
from __future__ import annotations
import os, sys, io
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psycopg2
from factor_ic_scan import load_creds

DDL = """
CREATE TABLE IF NOT EXISTS factor_value (
    factor_id    TEXT      NOT NULL,
    trade_date   DATE      NOT NULL,
    symbol       TEXT      NOT NULL,
    raw_value    NUMERIC,
    z_value      NUMERIC,
    available_at DATE      NOT NULL,
    version      TEXT      NOT NULL DEFAULT '1.0',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (factor_id, trade_date, symbol)
)
"""

FACTOR_COLS = "factor_id, trade_date, symbol, raw_value, z_value, available_at, version"


def main():
    # 本地源库（默认 127.0.0.1:5432，凭据取自 .env）
    src = psycopg2.connect(**load_creds())
    scur = src.cursor()
    scur.execute("SELECT count(*) FROM factor_value")
    n_src = scur.fetchone()[0]
    print(f"[src] 本地 factor_value = {n_src:,} 行")
    if n_src == 0:
        print("[abort] 本地 factor_value 为空，跳过云端同步（不误清空云端）")
        return

    # 云端目标库（隧道端口）
    os.environ["POSTGRES_PORT"] = os.environ.get("DST_PGPORT", "15432")
    try:
        dst = psycopg2.connect(**load_creds())
    except Exception as e:  # noqa: BLE001
        print(f"[error] 无法连接云端（隧道端口 {os.environ['POSTGRES_PORT']}）：{e}")
        print("        请确认 ssh -L 隧道已建立，或用 DST_PGPORT= 指定实际本地端口。")
        return

    try:
        dcur = dst.cursor()
        dcur.execute(DDL)
        dcur.execute("TRUNCATE factor_value")
        buf = io.StringIO()
        scur.copy_expert(f"COPY (SELECT {FACTOR_COLS} FROM factor_value) TO STDOUT", buf)
        buf.seek(0)
        dcur.copy_expert(f"COPY factor_value ({FACTOR_COLS}) FROM STDIN", buf)
        dcur.execute("SELECT count(*) FROM factor_value")
        n_dst = dcur.fetchone()[0]
        dst.commit()
        print(f"[dst] 云端 factor_value = {n_dst:,} 行（已同步）")
    finally:
        dst.close()
        src.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
