"""
分钟→多周期合成器（#5 改造核心）
- 数据源：minute_bar（历史 CSV + 实时 tqsdk 1 分钟，统一口径）
- 产出：bar_5m / bar_15m / bar_30m / bar_60m（time_bucket 聚合，起点标签，Asia/Shanghai）
- 两种模式：
  * synthesize_bars_incremental：仅重算「最近 N 天」的桶，成本极低，供每日/每半小时调度；
    顺带覆盖「尚未收盘的在途桶」，保证最新根是定稿值。
  * synthesize_bars_full：全量 TRUNCATE + 重算，供一次性迁移/回填（需较大 work_mem）。

注：first()/last()/time_bucket() 均为 TimescaleDB 函数，plain table 同样可用。
合成口径与历史 gen_synth_chunk.py 完全一致（first(open,ts), max(high), min(low), last(close,ts)）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")

_BARS = [
    ("bar_5m", "5 minutes"),
    ("bar_15m", "15 minutes"),
    ("bar_30m", "30 minutes"),
    ("bar_60m", "60 minutes"),
]

_INSERT_HEAD = (
    "INSERT INTO {t} (symbol, bucket, open, high, low, close, volume, amount, open_interest)\n"
    "SELECT symbol,\n"
    "       time_bucket(INTERVAL '{iv}', ts, 'Asia/Shanghai') AS bucket,\n"
    "       first(open, ts), max(high), min(low), last(close, ts),\n"
    "       sum(volume)::bigint, sum(amount), last(open_interest, ts)\n"
    "FROM minute_bar\n"
)


def _run(session: Session, sql: str, params: dict | None = None) -> int:
    res = session.execute(text(sql), params or {})
    return int(getattr(res, "rowcount", -1))


def synthesize_bars_incremental(session: Session, days_back: int = 3) -> dict:
    """增量重算最近 days_back 天的桶（覆盖在途桶），成本极低。返回各表影响行数。

    边界处理：DELETE 按「桶对齐」阈值（含 since 所在的不完整桶），
    否则 since 落在某桶中间时该半截桶起点早于 since 不会被删、又因冲突被
    DO NOTHING 跳过，导致漏更新。
    """
    since = datetime.now(_SH_TZ) - timedelta(days=days_back)
    stats: dict[str, int] = {}
    for t, iv in _BARS:
        session.execute(
            text(
                f"DELETE FROM {t} WHERE bucket >= time_bucket(INTERVAL '{iv}', :since, 'Asia/Shanghai')"
            ),
            {"since": since},
        )
        sql = _INSERT_HEAD.format(t=t, iv=iv) + \
              "WHERE ts >= :since\nGROUP BY symbol, bucket\n" \
              "ON CONFLICT (symbol, bucket) DO NOTHING"
        stats[t] = _run(session, sql, {"since": since})
    session.commit()
    logger.info(f"[synth] incremental done: {stats}")
    return stats


def synthesize_bars_full(session: Session, work_mem: str = "2GB") -> dict:
    """全量重算（一次性迁移/回填）。耗时随分钟数据量线性增长。"""
    session.execute(text(f"SET LOCAL work_mem = '{work_mem}'"))
    stats: dict[str, int] = {}
    for t, _ in _BARS:
        session.execute(text(f"TRUNCATE {t}"))
    session.commit()
    for t, iv in _BARS:
        sql = _INSERT_HEAD.format(t=t, iv=iv) + "GROUP BY symbol, bucket\n" \
              "ON CONFLICT (symbol, bucket) DO NOTHING"
        stats[t] = _run(session, sql)
    session.commit()
    logger.info(f"[synth] full done: {stats}")
    return stats


__all__ = ["synthesize_bars_incremental", "synthesize_bars_full"]
