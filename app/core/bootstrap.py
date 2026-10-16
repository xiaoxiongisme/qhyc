"""
启动引导（R3②③）
- 将 config 中品种清单同步至 futures_symbol（幂等 upsert）
- 提供品种级 advisory lock（防 boot 与定时任务并发重复拉取）
"""
from __future__ import annotations

from contextlib import contextmanager

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


@contextmanager
def symbol_ingest_lock(symbol: str):
    """品种级 ingest 互斥锁（PG **会话级** advisory lock，专用连接持有）。

    用法::

        with symbol_ingest_lock(spec.symbol) as got:
            if not got:
                ...  # 该品种正被其他任务处理，本次跳过
            ...      # 正常采集流程

    ★ 2026-10-16 修（与 ``app/ingest/synthesizer.py::minute_pipeline_lock``
      同源事故，但 ingest 侧直到本次才修）：旧 ``try_symbol_lock`` /
      ``release_symbol_lock`` 在**业务 session** 上加锁/解锁，而
      ``ingest_symbol`` 内的 ``session.commit()`` 会把物理连接**归还连接池**，
      finally 里的解锁从池里重新 checkout——单线程顺序执行时 LIFO 通常拿回
      同一条连接（侥幸正确），但**并发作业同时取用连接池时**（如 20:00
      pre_night 与整点 alert check 相撞、boot ingest 与 fusion_scan 相撞）
      解锁就落到**未持锁的连接**上：``pg_advisory_unlock`` 返回 false 且
      **不抛异常**（旧代码连返回值都没看），锁随原连接**泄漏在池中**。
      后续 ingest 同品种时若 checkout 到该泄漏连接 → 恒被
      "locked by another ingest task" 静默跳过。

      2026-10-14~16 实测事故：4 条 idle 连接泄漏全部 50 个品种锁，
      ``ingest/midday`` 连续 3 天 partial（failed 23~31/50），触发心跳告警
      「作业 ingest（label=midday）已 3.0 天无成功运行」。

    修法（照 synthesizer 已证伪的同一模式）：锁的持有与释放固定在**同一条
    专用连接**（``get_engine().connect()``，AUTOCOMMIT，不参与业务事务、
    不经池轮换）；显式检查解锁返回值，失败即 ``pg_advisory_unlock_all()``
    兜底；连接最终关闭时 PG 也会自动释放会话级锁（双保险）。
    """
    from app.core.db import get_engine

    key = f"ingest:{symbol}"
    # AUTOCOMMIT：纯锁连接不挂事务，避免 idle-in-transaction
    with get_engine().connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as lock_conn:
        got = bool(lock_conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:s))"), {"s": key}
        ).scalar())
        try:
            yield got
        finally:
            if not got:
                return
            try:
                ok = bool(lock_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:s))"), {"s": key}
                ).scalar())
                if not ok:
                    # 持锁连接上解锁返回 false 只可能是代码路径被改坏——fail-loud
                    logger.error(
                        f"[lock] {symbol} 显式解锁返回 false（持锁连接异常），"
                        f"尝试 pg_advisory_unlock_all() 兜底"
                    )
                    lock_conn.execute(text("SELECT pg_advisory_unlock_all()"))
            except Exception as e:  # noqa: BLE001 —— 吞异常会导致锁永久泄漏
                logger.error(
                    f"[lock] {symbol} 显式解锁异常（{type(e).__name__}: {e}）；"
                    f"尝试 pg_advisory_unlock_all() 兜底"
                )
                try:
                    lock_conn.execute(text("SELECT pg_advisory_unlock_all()"))
                except Exception as e2:  # noqa: BLE001
                    logger.error(
                        f"[lock] {symbol} unlock_all 亦失败（{e2}）；"
                        f"连接关闭时 PG 将自动释放会话级锁"
                    )


def ingest_lock_holder(symbol: str) -> tuple[int, str, str] | None:
    """诊断用：返回当前持有该品种 ingest 锁的 (pid, state, backend_start)。

    取不到（未持锁/查询失败）返回 None。供「被其他任务锁定，跳过」时打点，
    避免再出现「锁泄漏数天但日志只有一行跳过」的盲区。
    """
    from app.core.db import get_engine

    key = f"ingest:{symbol}"
    try:
        with get_engine().connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            row = conn.execute(
                text(
                    "SELECT a.pid::text, a.state, a.backend_start::text "
                    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted "
                    "AND l.classid = CASE WHEN hashtext(:s) < 0 "
                    "     THEN 4294967295 ELSE 0 END "
                    "AND l.objid = (hashtext(:s)::bigint & 4294967295) "
                    "ORDER BY a.backend_start LIMIT 1"
                ),
                {"s": key},
            ).first()
        return (int(row[0]), row[1], row[2]) if row else None
    except Exception as e:  # noqa: BLE001 —— 诊断失败不阻断主流程
        logger.warning(f"[lock] 查询 {symbol} 持锁者失败: {e}")
        return None
