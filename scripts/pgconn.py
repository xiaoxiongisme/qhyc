# -*- coding: utf-8 -*-
"""scripts/ 下脚本的统一 PostgreSQL 连接入口（六层解耦 · 数据层门禁）。

设计意图
--------
历史上 `runtime/_adjust_*.py` 等脚本把 `host='127.0.0.1', port=5432` 与明文口令
硬编码在文件里，导致：
  1. 容器内跑时 127.0.0.1 不是数据库（scheduler 容器无 DB），必然失败且静默；
  2. 端口/口令分散在多个脚本，云端/本地切换靠改代码，无法复现部署。

本模块把连接参数收敛为「环境变量优先 > CLI 参数 > 内置默认」，使同一份脚本
在本地容器（POSTGRES_HOST=timescaledb）与云端容器（同名变量）都能不改代码运行。

环境变量（与 docker-compose 对齐）
---------------------------------
  POSTGRES_HOST      默认 127.0.0.1
  POSTGRES_PORT      默认 5432
  POSTGRES_USER      默认 futures
  POSTGRES_PASSWORD  **无默认**：缺失即抛错（fail-fast）
  POSTGRES_DB        默认 futures

同时提供两把作业级保护（针对 2026-09-27 实测的「bars 尾删重建 vs 复权撞车」竞态）：
  * `advisory_lock()`   —— 同一作业串行，拿不到锁直接退出而非并发破坏
  * `assert_source_not_regressed()` —— 源表尾部不得早于目标表已有尾部
"""

from __future__ import annotations

import os
from typing import Optional

class MissingPassword(RuntimeError):
    """未提供数据库口令（fail-fast，不静默用内置默认口令）。"""


def resolve_conn(host: Optional[str] = None, port: Optional[int] = None,
                 db: Optional[str] = None, user: Optional[str] = None,
                 pw: Optional[str] = None) -> dict:
    """按「CLI > 环境变量 > 默认」解析连接参数，返回 psycopg2.connect(**kwargs) 可用字典。

    ⚠ 口令**不再有内置默认**（R7）：此前在此硬编码开发库口令，使密钥随代码分发到
    数十个文件、且轮换需改代码。现改为：CLI > env > 缺失即抛
    :class:`MissingPassword`，避免"用旧默认静默连上/连不上"。
    """
    password = pw or os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise MissingPassword(
            "[fail-fast] 未提供数据库口令：请设置环境变量 POSTGRES_PASSWORD"
            "（或用 --pw 显式传入）。代码内不再保留默认口令。"
        )
    return dict(
        host=host or os.getenv("POSTGRES_HOST") or "127.0.0.1",
        port=int(port or os.getenv("POSTGRES_PORT") or 5432),
        user=user or os.getenv("POSTGRES_USER") or "futures",
        password=password,
        dbname=db or os.getenv("POSTGRES_DB") or "futures",
    )


def add_conn_args(ap):
    """给 argparse 补上标准连接参数（值留空表示交给环境变量）。"""
    ap.add_argument("--host", default=None, help="默认取 POSTGRES_HOST")
    ap.add_argument("--port", type=int, default=None, help="默认取 POSTGRES_PORT")
    ap.add_argument("--db", default=None, help="默认取 POSTGRES_DB")
    ap.add_argument("--user", default=None, help="默认取 POSTGRES_USER")
    ap.add_argument("--pw", default=None, help="默认取 POSTGRES_PASSWORD")
    return ap


def conn_from_args(a) -> dict:
    return resolve_conn(getattr(a, "host", None), getattr(a, "port", None),
                        getattr(a, "db", None), getattr(a, "user", None),
                        getattr(a, "pw", None))


# --------------------------------------------------------------------------
# 作业级保护
# --------------------------------------------------------------------------

def advisory_lock(cur, key: str, timeout_sec: int = 0) -> bool:
    """拿作业级咨询锁。拿不到返回 False（调用方应直接退出，不要并发写）。

    key 会经 hashtext 转成 64 位整数；同一 key 在同一库内互斥。
    """
    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,))
    return bool(cur.fetchone()[0])


def advisory_unlock(cur, key: str) -> None:
    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))


class SourceRegressed(Exception):
    """源表尾部早于目标表已有尾部 —— 典型的「上游尾删重建未完成」竞态。"""


def assert_source_not_regressed(cur, src_sql: str, dst_sql: str,
                                label: str = "") -> tuple:
    """校验源表最新时间戳不得早于目标表已有最新时间戳。

    src_sql / dst_sql 需各返回单列单行的 max 时间戳（可为 NULL）。

    背景：bars 增量合成作业会「尾删再重建」bar_* 尾部。若在重建中途跑复权，
    会读到残缺源并静默产出「缺最近几天」的 cont_adj（2026-09-27 实测复现）。
    此处直接抛异常中止，避免脏数据入库。
    """
    cur.execute(src_sql)
    r = cur.fetchone()
    src_max = r[0] if r else None
    cur.execute(dst_sql)
    r = cur.fetchone()
    dst_max = r[0] if r else None
    if src_max is None:
        raise SourceRegressed(f"{label} 源表为空，拒绝执行")
    if dst_max is not None and src_max < dst_max:
        raise SourceRegressed(
            f"{label} 源尾部 {src_max} 早于目标已有尾部 {dst_max} "
            f"—— 疑似上游尾删重建未完成，拒绝写入以免产生残缺数据")
    return src_max, dst_max
