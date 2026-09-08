"""
CLI：推断交易日历（R2，M1 降级方案）
- 品种间交叉：当日 >=80% 主连品种有数据 → 视为交易日
- 写入 trade_calendar（exchange='INF'，与官方日历分列）
- 用法：docker compose exec api python scripts/infer_calendar.py [--ratio 0.8]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.ingest.calendar_infer import calendar_mode, infer_calendar


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--ratio", type=float, default=0.8)
    args = ap.parse_args()

    with session_scope() as s:
        stats = infer_calendar(s, min_ratio=args.ratio)
        logger.info(f"日历模式: {calendar_mode(s)}")
        logger.info(f"结果: {stats}")


if __name__ == "__main__":
    main()
