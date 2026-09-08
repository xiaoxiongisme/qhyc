"""仓储通用：upsert + range query

注：TimescaleDB 超表（daily_bar / main_continuous / hourly_bar）已建立
唯一主键 `(symbol, trade_date)` / `(product, trade_date)`，故可走
PostgreSQL 原生 `INSERT ... ON CONFLICT ... DO UPDATE`。

分块说明：psycopg3 扩展协议单条语句参数上限 65535，
大批量写入需按列数切分（见 _chunked）。
"""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import DailyBar, MainContinuous, HourlyBar

# psycopg3 扩展协议单语句参数上限 65535，留安全余量
_MAX_PARAMS = 60000


def _chunked(rows: Sequence[dict], ncols: int) -> list[list[dict]]:
    """按列数切分行块，保证每条语句参数数 < 65535"""
    size = max(1, _MAX_PARAMS // max(1, ncols))
    return [list(rows[i : i + size]) for i in range(0, len(rows), size)]


def upsert_daily_bars(session: Session, rows: Sequence[dict]) -> int:
    """upsert daily_bar（按主键 (symbol, trade_date)），自动分块"""
    if not rows:
        return 0
    ncols = 15  # 全字段列数
    for chunk in _chunked(rows, ncols):
        stmt = pg_insert(DailyBar).values(chunk)
        update_cols = {
            c: stmt.excluded[c]
            for c in (
                "open",
                "high",
                "low",
                "close",
                "settle",
                "volume",
                "amount",
                "oi",
                "ret_close",
                "ret_settle",
                "ret5",
                "ret20",
                "src",
                "updated_at",
            )
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "trade_date"], set_=update_cols
        )
        session.execute(stmt)
    return len(rows)


def upsert_main_continuous(session: Session, rows: Sequence[dict]) -> int:
    """upsert main_continuous（按主键 (product, trade_date)），自动分块"""
    if not rows:
        return 0
    ncols = 18
    for chunk in _chunked(rows, ncols):
        stmt = pg_insert(MainContinuous).values(chunk)
        update_cols = {
            c: stmt.excluded[c]
            for c in (
                "raw_open",
                "raw_high",
                "raw_low",
                "raw_close",
                "raw_volume",
                "raw_oi",
                "adj_open",
                "adj_high",
                "adj_low",
                "adj_close",
                "adj_volume",
                "adj_oi",
                "underlying",
                "change_flag",
                "src",
            )
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["product", "trade_date"], set_=update_cols
        )
        session.execute(stmt)
    return len(rows)


def upsert_hourly_bars(session: Session, rows: Sequence[dict]) -> int:
    """upsert hourly_bar（按主键 (symbol, trade_datetime)），自动分块"""
    if not rows:
        return 0
    ncols = 9
    for chunk in _chunked(rows, ncols):
        stmt = pg_insert(HourlyBar).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "trade_datetime"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "oi": stmt.excluded.oi,
            },
        )
        session.execute(stmt)
    return len(rows)


def fetch_existing_keys(session: Session, model, key_columns: Sequence[str]) -> set[tuple]:
    """取已存在的主键集合（用于缺失检测）"""
    cols = [getattr(model.c, c) for c in key_columns]
    rows = session.execute(select(*cols)).all()
    return {tuple(r) for r in rows}


__all__ = [
    "upsert_daily_bars",
    "upsert_main_continuous",
    "upsert_hourly_bars",
    "fetch_existing_keys",
]