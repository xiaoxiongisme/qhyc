"""
DB 健康检查 + TimescaleDB 扩展确认
- 等待 DB 可达
- 校验扩展、超表存在
- M1 阶段 01_schema.sql 由 docker-entrypoint-initdb.d 自动跑；
  本脚本用于运行期 sanity check。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.core.db import get_engine
from app.core.logging import logger, setup_logging


def wait_for_db(max_wait: int = 60) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            logger.info("[init_db] DB is ready")
            return True
        except Exception as e:
            logger.info(f"[init_db] waiting DB: {e}")
            time.sleep(2)
    logger.error("[init_db] DB not ready after timeout")
    return False


def main():
    setup_logging()
    if not wait_for_db():
        sys.exit(1)

    with get_engine().connect() as c:
        ext = c.execute(
            text("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        ).all()
        logger.info(f"[init_db] extensions: {ext}")
        if not any(r[0] == "timescaledb" for r in ext):
            logger.error("[init_db] timescaledb 扩展未启用")
            sys.exit(1)

        ht = c.execute(
            text("SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1")
        ).all()
        logger.info(f"[init_db] hypertables: {[r[0] for r in ht]}")
        for need in ("daily_bar", "main_continuous", "hourly_bar"):
            if not any(r[0] == need for r in ht):
                logger.error(f"[init_db] missing hypertable: {need}")
                sys.exit(1)

        n_symbol = c.execute(text("SELECT count(*) FROM futures_symbol")).scalar()
        logger.info(f"[init_db] futures_symbol rows = {n_symbol}")

    logger.info("[init_db] OK")


if __name__ == "__main__":
    main()