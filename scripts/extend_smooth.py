"""
CLI：平滑主连自动延伸（M2.1）
- 用法：docker compose exec api python scripts/extend_smooth.py [--product FG]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.ingest.smooth_extender import extend_all


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", type=str, default=None, help="品种（缺省=全部有 CSV 口径的品种）")
    args = ap.parse_args()

    products = [args.product.upper()] if args.product else None
    with session_scope() as s:
        results = extend_all(s, products=products)
    for r in results:
        logger.info(f"延伸结果: {r}")


if __name__ == "__main__":
    main()
