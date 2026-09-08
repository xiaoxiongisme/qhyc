"""
启动引导（R3②③）
- 将 config 中品种清单同步至 futures_symbol（幂等 upsert）
- 提供品种级 advisory lock（防 boot 与定时任务并发重复拉取）
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger


def sync_symbols(session: Session) -> int:
    """把 config.ingest.main_contracts 同步到 futures_symbol（幂等）

    M1 收尾（R3②）：目标品种清单以 config 为唯一事实源，
    新增品种改 config 即可，无需手写 SQL 种子。
    """
    settings = get_settings()
    specs = settings.main_contracts
    if not specs:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.models import FuturesSymbol

    rows = [
        {
            "symbol": s.symbol,
            "name": s.name,
            "exchange": s.exchange,
            "unit": s.unit,
            "multiplier": s.multiplier,
            "product": s.product,
            "is_main": True,
            "main_symbol": s.symbol,
            "active": True,
        }
        for s in specs
    ]
    stmt = pg_insert(FuturesSymbol).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol"],
        set_={
            "name": stmt.excluded.name,
            "exchange": stmt.excluded.exchange,
            "unit": stmt.excluded.unit,
            "multiplier": stmt.excluded.multiplier,
            "product": stmt.excluded.product,
            "is_main": True,
            "main_symbol": stmt.excluded.main_symbol,
            "active": True,
            "updated_at": text("NOW()"),
        },
    )
    session.execute(stmt)
    session.commit()
    logger.info(f"[bootstrap] futures_symbol synced {len(rows)} symbols from config")

    # §16.1 大类分类同步（transmission_config.yaml → sector_map）
    try:
        from app.sectors.builder import sync_sector_map

        sync_sector_map(session)
    except Exception as e:
        logger.warning(f"[bootstrap] sector_map sync failed: {e}")
    return len(rows)


def try_symbol_lock(session: Session, symbol: str) -> bool:
    """R3③ 品种级锁：pg_try_advisory_lock（会话级，跨事务提交持续有效）

    True=拿到锁可执行；False=该品种正被其他任务处理，本次跳过。
    配对 release_symbol_lock() 释放（或 session 关闭时自动释放）。
    """
    res = session.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:s))"), {"s": f"ingest:{symbol}"}
    ).scalar()
    return bool(res)


def release_symbol_lock(session: Session, symbol: str) -> None:
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(hashtext(:s))"), {"s": f"ingest:{symbol}"}
        )
    except Exception as e:
        logger.warning(f"[lock] unlock {symbol} 异常: {e}")
