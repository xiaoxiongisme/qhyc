"""
CLI：传导动态权重周算（§16.2 第 2 层）+ 层级预测（第 3 层）
- 用法：docker compose exec api python scripts/calc_transmission.py [--skip-hierarchy]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db import session_scope
from app.core.logging import logger, setup_logging


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-hierarchy", action="store_true", help="跳过大类指数层级预测")
    args = ap.parse_args()

    with session_scope() as s:
        from app.sectors.dynamic_weights import calc_dynamic_weights

        stats = calc_dynamic_weights(s)
        logger.info(f"动态权重: {stats}")

        if not args.skip_hierarchy:
            from app.engine.service import predict_sector_indices

            outs = predict_sector_indices(s)
            for o in outs:
                logger.info(
                    f"层级预测 {o['symbol']} -> {o['direction']} p={o['direction_prob']} "
                    f"target={o['target_date']}"
                )


if __name__ == "__main__":
    main()