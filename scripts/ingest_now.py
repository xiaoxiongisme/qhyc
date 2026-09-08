"""
CLI：手动触发一次全量 ingest（akshare + tqsdk 校准）
- 用法：
  docker compose exec api python scripts/ingest_now.py [--symbol FG888]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.ingest.orchestrator import IngestOrchestrator
from app.repositories.task_repo import TaskRepository


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", type=str, default=None)
    ap.add_argument("--skip-calibration", action="store_true")
    args = ap.parse_args()

    settings = get_settings()

    with session_scope() as s:
        repo = TaskRepository(s)
        run = repo.start("ingest", label="cli-manual", payload={"args": vars(args)})
        orch = IngestOrchestrator(s)

        if args.symbol:
            spec = next((x for x in settings.main_contracts if x.symbol == args.symbol), None)
            if not spec:
                repo.finish(run, "failed", f"unknown symbol {args.symbol}")
                sys.exit(1)
            reports = [orch.ingest_symbol(spec, skip_calibration=args.skip_calibration)]
        else:
            reports = orch.ingest_all()

        failed = [r for r in reports if r.error]
        repo.finish(
            run,
            "success" if not failed else "partial",
            f"total={len(reports)} failed={len(failed)}",
        )
        for r in reports:
            logger.info(r.to_dict())


if __name__ == "__main__":
    main()