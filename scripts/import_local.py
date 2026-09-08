"""
CLI：手动触发本地历史导入
- 用法：
  docker compose exec api python scripts/import_local.py [--product FG]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.ingest.local_importer import LocalHistoryImporter


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", type=str, default=None, help="品种，如 FG；缺省=全部")
    args = ap.parse_args()

    with session_scope() as s:
        imp = LocalHistoryImporter(s)
        if args.product:
            stats = imp.import_product(args.product)
            logger.info(f"导入 {args.product}: {stats}")
        else:
            results = imp.import_all()
            for r in results:
                logger.info(f"导入结果: {r}")


if __name__ == "__main__":
    main()