# -*- coding: utf-8 -*-
"""Manual backfill for inventory (L0 collection layer).

Replicates the logic of app.scheduler._inventory_job but runs as a one-shot
standalone process (no APScheduler dependency). Writes to DB and logs a
task_run record of type 'inventory' (label 'manual_backfill_20261009').

Run on cloud:
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc APP_ENV=cloud /home/ubuntu/qhyc_work/venv/bin/python \
        scripts/ops/manual_inventory_backfill.py
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.db import session_scope
from app.repositories.task_repo import TaskRepository
from app.ingest import inventory as INV


def main() -> None:
    settings = get_settings()
    products = [spec.product for spec in settings.main_contracts]
    print(f"[backfill] products to collect: {len(products)}", flush=True)
    with session_scope() as s:
        repo = TaskRepository(s)
        run = repo.start("inventory", label="manual_backfill_20261009")
        try:
            stats = INV.collect_inventory_em(s, products)
            repo.finish(run, "success", str(stats))
            print(f"[backfill] STATS: {stats}", flush=True)
        except Exception as e:  # noqa: BLE001
            repo.finish(run, "failed", str(e))
            print(f"[backfill] FAILED: {e!r}", flush=True)
            raise


if __name__ == "__main__":
    main()
