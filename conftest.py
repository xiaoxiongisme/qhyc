"""pytest 根配置（放在仓库根目录，pytest 会自动加载）。

作用：把 ``scripts/`` 加入 ``sys.path``，使 tests 可直接 import 其中的脚本级模块。

背景（2026-09-29 实测）：``tests/test_lookahead_guard.py`` 以顶层 import 方式引用
``compute_factor_v1v6``（位于 ``scripts/``，它是脚本而非 ``app`` 包内模块）。
未补 sys.path 时：

    python -m pytest tests/
    → ModuleNotFoundError: No module named 'compute_factor_v1v6'
    → Interrupted: 1 error during collection      # 注意：是**整轮 0 用例运行**，不是跳过

补上后同一条命令应全绿。容器内的等价路径为 ``/app/scripts``（Dockerfile COPY scripts）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

for _p in (_ROOT, _ROOT / "scripts"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

# ---------------------------------------------------------------------------
# requires_db：DB 集成测试边界（Feathers「切断依赖」，使套件在任意环境可跑）
#
# 大量集成测试（test_execution_broker / test_roll_policy_caliber / engine_rebind
# 的部分用例）需要**可达且已 seed 的 Postgres**。dev 机默认未设 DATABASE_URL、
# 且本地无 PG 监听（默认主机名 timescaledb 仅容器内可解析），直接跑会抛
# KeyError / OperationalError，污染整套结果。
#
# 引入该 seam：无 DB 时自动 skip（清晰标注原因），有 DB 时照常运行。
# 启用方式：export DATABASE_URL='postgresql+psycopg://user:pw@host:5432/db'
# （云端可经 15432 SSH 隧道指向全量库；注意 test_execution_broker 会写
#   execution_* 表，不要在共享生产库上跑 B 系列用例）。
import os

import pytest
from sqlalchemy import create_engine, text


def _db_available() -> bool:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        with create_engine(url, pool_pre_ping=True).connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def pytest_collection_modifyitems(config, items):
    if _db_available():
        return
    skip = pytest.mark.skip(
        reason="requires_db: 需要可达且已 seed 的 Postgres（设置 DATABASE_URL）；"
               "当前环境无 DB，按 Feathers 法跳过集成测试")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip)
