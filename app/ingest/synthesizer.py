"""
分钟→多周期合成器（#5 改造核心）
- 数据源：minute_bar（历史 CSV + 实时 akshare 1 分钟，统一口径）
- 产出：bar_5m / bar_15m / bar_30m / bar_60m（time_bucket 聚合，起点标签，Asia/Shanghai）
- 两种模式：
  * synthesize_bars_incremental：仅重算「最近 N 天」的桶，成本极低，供每日/每半小时调度；
    顺带覆盖「尚未收盘的在途桶」，保证最新根是定稿值。
  * synthesize_bars_full：全量 TRUNCATE + 重算，供一次性迁移/回填（需较大 work_mem）。

注：first()/last()/time_bucket() 均为 TimescaleDB 函数，plain table 同样可用。
合成口径与历史 gen_synth_chunk.py 完全一致（first(open,ts), max(high), min(low), last(close,ts)）。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")

#: 分钟写入（MinuteCollector）与桶合成之间的**互斥锁键**。
#: 两者必须串行：并发时合成会读到 minute_bar 的中间快照，导致最新交易日整段漏桶
#: （2026-10-06 实测，见 synthesize_bars_incremental 的说明）。
LOCK_MINUTE_PIPELINE = "qhyc_minute_bar_pipeline"

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


@contextmanager
def minute_pipeline_lock(session: Session, timeout_sec: int = 600):
    """分钟写入 ↔ 桶合成的互斥锁（PG 会话级 advisory lock）。

    用法::

        with minute_pipeline_lock(s):
            ...  # 采集 minute_bar 或合成 bar_* 二者之一

    拿不到锁时**等待**（``timeout_sec``）而非静默跳过——宁可串行也不产出残缺桶。
    """
    key = LOCK_MINUTE_PIPELINE
    deadline = time.time() + timeout_sec
    got = False
    while True:
        got = bool(session.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}
        ).scalar())
        if got or time.time() >= deadline:
            break
        logger.warning("[synth] 分钟管线锁被占用，等待中…")
        time.sleep(2.0)
    if not got:
        # fail-loud：绝不「拿不到锁就照跑」制造残缺数据
        raise TimeoutError(f"[synth] 等待分钟管线锁超时({timeout_sec}s)，拒绝并发合成")
    try:
        yield
    finally:
        try:
            session.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()


def _max_bucket(session: Session, table: str) -> datetime | None:
    return session.execute(text(f"SELECT max(bucket) FROM {table}")).scalar()


def _max_minute_ts(session: Session) -> datetime | None:
    return session.execute(text("SELECT max(ts) FROM minute_bar")).scalar()


def synthesize_bars_incremental(session: Session, days_back: int = 3,
                                max_retries: int = 2) -> dict:
    """增量重算最近 days_back 天的桶（覆盖在途桶），成本极低。返回各表影响行数。

    边界处理：DELETE 按「桶对齐」阈值（含 since 所在的不完整桶），
    否则 since 落在某桶中间时该半截桶起点早于 since 不会被删、又因冲突被
    DO NOTHING 跳过，导致漏更新。

    ★并发校验重试（2026-10-06 新增）
    ------------------------------------
    背景：本作业与 `MinuteCollector`（每 30 分钟写 minute_bar）**并发**。若合成
    执行期间 minute_bar 正在追加当天分钟线，则「DELETE 已删最近桶 → INSERT 读到
    尚未提交的快照」会把最新交易日**整段漏掉**（实测 09-30 一度回退到 09-29，
    脚本自身 rowcount 仍为正、**不报错**，属静默数据丢失）。

    对策：落库后核对「每张表 max(bucket) 是否已追平 minute_bar 的 max(ts)
    所在桶」，未追平则重跑（最多 ``max_retries`` 次）。仍不追平则 **warning 明示**，
    绝不静默返回成功——调用方与告警都能看见。
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

    # ---- 落库后校验：bar_* 是否已追平 minute_bar 最新所在桶 ----
    lags: dict[str, int] = {}
    for attempt in range(1, max_retries + 1):
        target = _max_minute_ts(session)
        if target is None:
            break
        # 每张表期望追平到「minute_bar 最新 ts 所在的桶起点」
        pending = {}
        for t, iv in _BARS:
            exp = session.execute(
                text(f"SELECT time_bucket(INTERVAL '{iv}', :ts, 'Asia/Shanghai')"),
                {"ts": target},
            ).scalar()
            got = _max_bucket(session, t)
            if exp is not None and (got is None or got < exp):
                pending[t] = (exp, got)
        if not pending:
            break
        lags = {t: f"期望≥{e} 实际{g}" for t, (e, g) in pending.items()}
        logger.warning(
            f"[synth] 校验未通过（第{attempt}/{max_retries}次重试）：{lags}；"
            f"疑似 minute_bar 正在被 MinuteCollector 并发写入")
        if attempt == max_retries:
            break
        # 重跑：扩大窗口重算，覆盖刚被并发写入的分钟线
        since = datetime.now(_SH_TZ) - timedelta(days=days_back)
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

    if lags:
        logger.error(f"[synth] 重试后仍未追平 minute_bar（可能 MinuteCollector 持续并发写入）: {lags}")
    else:
        logger.info("[synth] 校验通过：bar_* 已追平 minute_bar 最新桶")
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


__all__ = ["synthesize_bars_incremental", "synthesize_bars_full",
           "minute_pipeline_lock", "LOCK_MINUTE_PIPELINE"]
