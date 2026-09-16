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


def _bar_row_is_valid(row: dict) -> bool:
    """剔除脏行：tqsdk 回填时未填充的槽位会给出 1970-01-01 时间戳 / NaN 价。"""
    dt = row.get("trade_datetime")
    if dt is None or getattr(dt, "year", 9999) <= 1970:
        return False
    for k in ("open", "high", "low", "close"):
        v = row.get(k)
        if v is None:
            return False
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        if fv != fv or fv in (float("inf"), float("-inf")):   # NaN / ±Inf
            return False
    return True


def upsert_hourly_bars(session: Session, rows: Sequence[dict]) -> int:
    """upsert hourly_bar（按主键 (symbol, trade_datetime)），自动分块。

    同一批 rows 内可能含重复主键（tqsdk 回填的未填充槽位大量落在同一时间戳），
    而 `INSERT ... ON CONFLICT DO UPDATE` 不允许同一命令里重复命中同一行
    （psycopg `CardinalityViolation: cannot affect row a second time`）——
    故先按主键去重（后出现的覆盖先出现的），并剔除 1970/NaN 脏行。
    """
    if not rows:
        return 0
    dedup: dict[tuple, dict] = {}
    for r in rows:
        if not _bar_row_is_valid(r):
            continue
        dedup[(r.get("symbol"), r.get("trade_datetime"))] = r
    clean = list(dedup.values())
    if not clean:
        return 0
    ncols = 9
    for chunk in _chunked(clean, ncols):
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
    return len(clean)


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