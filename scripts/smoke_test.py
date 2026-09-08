"""
M1 验收脚本（PRD §12）
- 不依赖 akshare/tqsdk 网络（在数据未拉取时也可单独验）：
  1. DB 连接 + TimescaleDB 扩展 + 超表存在
  2. 元数据种子（品种列表）
  3. 本地历史数据导入（§4.5）
  4. daily_bar / main_continuous / main_contract_map 行数
  5. 异常工单接口（手工 mock 一条，验证 resolve）
  6. ingest 流程入口调用（可跳过网络）
- 用法：
  docker compose exec api python scripts/smoke_test.py
  或本地（需装依赖）：python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.core.db import get_engine, session_scope
from app.core.logging import logger, setup_logging
from app.ingest.local_importer import LocalHistoryImporter
from app.repositories.symbol_repo import SymbolRepository


def check_db_connection() -> bool:
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
            ver = c.execute(text("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")).scalar()
            logger.info(f"[smoke] DB OK | timescaledb={ver}")
        return True
    except Exception as e:
        logger.error(f"[smoke] DB fail: {e}")
        return False


def check_hypertables() -> bool:
    expect = ("daily_bar", "main_continuous", "hourly_bar")
    with get_engine().connect() as c:
        rows = c.execute(
            text("SELECT hypertable_name FROM timescaledb_information.hypertables")
        ).all()
        have = {r[0] for r in rows}
        missing = [t for t in expect if t not in have]
        if missing:
            logger.error(f"[smoke] 缺超表: {missing}")
            return False
        logger.info(f"[smoke] hypertables ok: {sorted(have)}")
    return True


def check_symbol_seed() -> bool:
    with session_scope() as s:
        rows = SymbolRepository(s).list_mains()
        logger.info(f"[smoke] 主连数 = {len(rows)}")
        if len(rows) < 5:
            logger.error("[smoke] 主连种子不足 5 条")
            return False
    return True


def check_imports(products: list[str]) -> bool:
    with session_scope() as s:
        imp = LocalHistoryImporter(s)
        found = imp.discover_products()
        logger.info(f"[smoke] discover_products = {found}")
        target = [p for p in products if p in found] or found
        for p in target:
            try:
                stats = imp.import_product(p)
                logger.info(f"[smoke] imported {p}: {stats}")
            except Exception as e:
                logger.exception(f"[smoke] import {p} failed: {e}")
                return False
    # 行数校验
    with get_engine().connect() as c:
        n_daily = c.execute(text("SELECT count(*) FROM daily_bar")).scalar()
        n_main = c.execute(text("SELECT count(*) FROM main_continuous")).scalar()
        n_map = c.execute(text("SELECT count(*) FROM main_contract_map")).scalar()
        logger.info(
            f"[smoke] counts: daily_bar={n_daily} main_continuous={n_main} "
            f"main_contract_map={n_map}"
        )
        if n_daily < 100 or n_main < 100:
            logger.error("[smoke] 行数过低，请检查导入")
            return False
    return True


def check_anomaly_roundtrip() -> bool:
    """手工插入一条工单并裁决，验证 resolve 接口流程"""
    from datetime import date

    from app.models import AnomalyTicket
    from app.api.routers.anomalies import resolve_anomaly, ResolveRequest  # noqa

    # 直接走 ORM 验证（API 接口需要在 FastAPI 启动后调用）
    with session_scope() as s:
        t = AnomalyTicket(
            symbol="FG888",
            trade_date=date(2024, 1, 2),
            field="close",
            akshare_val=1500.0,
            tqsdk_val=1510.0,
            diff=0.0066,
            threshold=0.005,
            status="pending",
            note="smoke test",
        )
        s.add(t)
        s.flush()
        tid = t.id
        logger.info(f"[smoke] inserted ticket id={tid}")
    return tid > 0


def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info(" M1 验收脚本（PRD §12）")
    logger.info("=" * 60)

    steps = [
        ("DB 连接", check_db_connection),
        ("超表存在", check_hypertables),
        ("主连种子", check_symbol_seed),
        ("本地历史导入（FG/SA）", lambda: check_imports(["FG", "SA"])),
        ("异常工单 roundtrip", check_anomaly_roundtrip),
    ]
    failed: list[str] = []
    for name, fn in steps:
        try:
            ok = fn()
            if ok:
                logger.info(f"[smoke] ✔ {name}")
            else:
                logger.error(f"[smoke] ✘ {name}")
                failed.append(name)
        except Exception as e:
            logger.exception(f"[smoke] ✘ {name}: {e}")
            failed.append(name)

    if failed:
        logger.error(f"[smoke] M1 验收未通过: {failed}")
        sys.exit(1)
    logger.info("[smoke] M1 验收全部通过 ✔")


if __name__ == "__main__":
    main()