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

#: 冲突时的覆盖子句（2026-10-08 新增，配合「纯 upsert」改造）。
#:
#: 为什么必须是``DO UPDATE`` 而不是 ``DO NOTHING``：
#:   增量合成要修的是「**在途桶**」——同一根 bar 会在采集过程中被多次重算
#:   （分钟线陆续到齐 → 桶的 OHLC/volume 变化）。``DO NOTHING`` 会让**首次写入
#:   那个不完整版本被永久固化**（���后的重算全部冲突跳过），这正是原实现必须靠
#:   「先 DELETE 再 INSERT」才能修正的原因。改成 ``DO UPDATE`` 后：
#:     · 在途桶每次重算都被刷新 → 不再需要 DELETE；
#:     · 源数据被修正时重跑即自愈；
#:     · 没有任何"桶消失"窗口。
#: 代价：并发下最后写入者胜（与原先 DELETE+INSERT 的最终态一致）。
_UPSERT_TAIL = (
    "ON CONFLICT (symbol, bucket) DO UPDATE SET\n"
    "    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,\n"
    "    close=EXCLUDED.close, volume=EXCLUDED.volume,\n"
    "    amount=EXCLUDED.amount, open_interest=EXCLUDED.open_interest\n"
)


def _run(session: Session, sql: str, params: dict | None = None) -> int:
    res = session.execute(text(sql), params or {})
    return int(getattr(res, "rowcount", -1))


def advisory_lock_holder() -> int | None:
    """返回当前持有分钟管线锁的 backend pid；无人持有则 ``None``。

    诊断用（2026-10-08 新增）。用于两件事：

    1. 作业失败时区分「锁被别人正常持有」与「锁已**泄漏**」——后者会让
       ``minute_and_bars`` 永久 600s 超时、``bar_*`` 静默停更。
    2. 事后排查：泄漏的锁表现为「持锁 session 处于 ``idle`` 且``state_change``
       很久以前」，即它对应的作业早已结束。

    ⚠ 判据要留意：PostgreSQL 的会话级 advisory lock 随**物理连接**存活，
      而 SQLAlchemy 的连接池会**复用**连接，所以「持有锁的 pid」未必等于
      「正在跑作业的进程」——池里被复用的连接仍可能带着上一轮残留的锁。
    """
    from sqlalchemy import text

    from app.core.db import get_engine

    try:
        with get_engine().connect() as conn:
            return conn.execute(text(
                "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted "
                "AND objid = hashtext(:k) LIMIT 1"), {"k": LOCK_MINUTE_PIPELINE}
            ).scalar()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[synth] 查询 advisory 锁持有者失败: {e}")
        return None


def release_stale_pipeline_lock(*, idle_seconds: int = 900) -> int | None:
    """回收**泄漏**的分钟管线锁：持锁 session 已``idle`` 超过阈值则终止它。

    ★ 为什么需要（2026-10-09 实测事故）
    --------------------------------------
    会话级 advisory lock 只在「显式 unlock」或「物理连接关闭」时释放，而连接池
    会**复用**连接。一旦某轮``minute_and_bars`` 没能走到 unlock（进程被 kill、
    unlock 语句异常、或异常路径绕过），锁就永久挂起 → 后续每轮都 600s 超时 →
    ``bar_*`` **静默停止更新**，而日志只有一行「等待分钟管线锁超时」。

    判据：持锁者 ``state='idle'`` 且 ``state_change`` 早于 ``idle_seconds``
    ——即它对应的作业早已结束，锁纯属残留。
    正常作业执行期间连接是 ``active``，不会被误杀。

    Returns
    -------
    被终止的 pid；无泄漏则 ``None``。
    """
    from sqlalchemy import text

    from app.core.db import get_engine

    try:
        with get_engine().connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            row = conn.execute(text(
                "SELECT a.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype='advisory' AND l.granted "
                "  AND l.objid = hashtext(:k) "
                "  AND a.state = 'idle' "
                "  AND now() - a.state_change > make_interval(secs => :s) "
                "LIMIT 1"), {"k": LOCK_MINUTE_PIPELINE, "s": idle_seconds}
            ).fetchone()
            if not row:
                return None
            pid = row[0]
            ok = conn.execute(
                text("SELECT pg_terminate_backend(:p)"), {"p": pid}).scalar()
            if ok:
                logger.error(
                    f"[synth] 回收泄漏的分钟管线锁：终止 backend pid={pid} "
                    f"（已 idle 超 {idle_seconds}s，判定为残留锁）。"
                    f"若近期无 manual 重启，此举可避免 bar_* 静默停更。")
            return pid if ok else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[synth] 回收泄漏锁失败: {e}")
        return None


@contextmanager
def minute_pipeline_lock(timeout_sec: int = 600):
    """分钟写入 ↔ 桶合成的互斥锁（PG **会话级** advisory lock）。

    用法::

        with minute_pipeline_lock():
            ...  # 采集 minute_bar 或合成 bar_* 二者之一

    拿不到锁时**等待**（``timeout_sec``）而非静默跳过——宁可串行也不产出残缺桶。

    ★ 2026-10-08 修（PR review I1，致命）：锁的**持有连接**必须与业务 session
      分离。此前在业务 ``session`` 上取锁，而``yield`` 体内
      :func:`collect_all` 与 :func:`synthesize_bars_incremental` 都会
      ``session.commit()`` —— 提交会把**物理连接归还连接池**，后续
      ``execute`` 从池里重新 checkout。因此：

        · 取锁在连接 A、``pg_advisory_unlock`` 极可能落在连接 B/C
          → 在未持锁的连接上解锁**返回 false 且不报错**（返回值被丢弃）；
        · 连接 A 带着锁回到池中→ 后续作业 checkout 到它时
          ``pg_try_advisory_lock`` **立即返回 true**（同会话重复加锁）；
        · 结论：**跨进程互斥彻底失效**，而该锁的唯一职责正是防
          「DELETE 已删最近桶 → INSERT 读到未提交快照 → 最新交易日整段漏桶」
          —— 即失效＝静默数据丢失的防护失效。

      修法：用**独立连接**持锁（``engine.connect()``），不参与业务事务；
      退出时在同一连接解锁；即便异常路径漏了解锁，连接关闭时 PG 会自动释放
      会话级锁（不会泄漏）。
    """
    from app.core.db import get_engine

    key = LOCK_MINUTE_PIPELINE
    deadline = time.time() + timeout_sec
    got = False
    # ★ 独立连接：与业务 session 完全解耦，commit/归还连接池不影响锁的归属
    with get_engine().connect() as lock_conn:
        while True:
            got = bool(lock_conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}
            ).scalar())
            if got or time.time() >= deadline:
                break
            logger.warning("[synth] 分钟管线锁被占用，等待中…")
            time.sleep(2.0)
        if not got:
            # fail-loud：绝不「拿不到锁就照跑」制造残缺数据
            #★ 先尝试回收「泄漏」的锁（持锁 session 已 idle 很久 = 上一轮残留）。
            #   2026-10-09 实测事故：残留锁让每轮都 600s 超时 → bar_* 静默停更，
            #   而日志只有一行「等待超时」。回收后重试一次，避免人工介入。
            stale = release_stale_pipeline_lock()
            if stale:
                logger.warning("[synth] 已回收泄漏锁(pid=%s)，重试一次…", stale)
                got = bool(lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}
                ).scalar())
            if not got:
                raise TimeoutError(
                    f"[synth] 等待分钟管线锁超时({timeout_sec}s)，拒绝并发合成"
                    f"（持锁 pid={advisory_lock_holder()}）。"
                    f"若该 pid 长期 idle，说明锁已泄漏。")
            yield_entered = True
        try:
            yield
        finally:
            # ★ 2026-10-08（PR review I1 实测踩到）：会话级 advisory lock 只在
            #   「显式 unlock」或「**物理连接关闭**」时释放；而 ``with engine.connect()``
            #   退出只是把连接**归还连接池** —— 物理连接仍存活，**锁不会自动释放**。
            #   于是只要 unlock 失败/未执行，锁就会**永久挂起**，后续每轮
            #   ``minute_and_bars`` 都会 600s 超时 → bar_* 数据从此停止更新（静默）。
            #   2026-10-09凌晨实测到此故障：pid93093 idle 持锁 20 分钟，
            #   最后一轮01:05 已结束（:07/:37 触发），确属泄漏。
            #
            # 因此这里必须：① 检查 unlock 的**返回值**（在未持锁的连接上返回 false
            # 且不抛异常，旧代码把返回值丢弃了）；② 失败即 error 并**主动清干净**。
            try:
                ok = lock_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key}
                ).scalar()
                if not ok:
                    # 该连接未持有锁：可能前一次 unlock 已成功、或是被复用到的连接
                    logger.info("[synth] 显式解锁返回 false（该连接未持锁，按已释放处理）")
            except Exception as e:  # noqa: BLE001
                # fail-loud：吞掉异常会导致锁永久泄漏 → bar_* 静默停更
                logger.error(f"[synth] 显式解锁异常（{type(e).__name__}: {e}）；"
                             f"尝试 pg_advisory_unlock_all() 兜底")
                try:
                    lock_conn.execute(text("SELECT pg_advisory_unlock_all()"))
                except Exception as e2:  # noqa: BLE001
                    # 连兜底都失败 → 只能是连接已断，物理关闭时 PG 会自动释放
                    logger.error(f"[synth] unlock_all 亦失败（{e2}）；"
                                 f"该连接关闭后 PG 将自动释放会话级锁")


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
        # ★ 2026-10-08 改为纯 upsert（原先 DELETE + INSERT DO NOTHING）。
        # 为什么：DELETE 与 INSERT 之间存在**数据消失窗口**——读者在此期间查询
        # 该表会看到"桶已删、尚未重算"的中间态。实测 2026-08-08 22:09 就撞到过
        # （`bar_*` 今日 0 行，22:11:11 才恢复）。虽然 DELETE/INSERT 同属一个事务、
        # 进程被杀会回滚，但把"可见的空窗"消掉更稳，且顺带获得两个性质：
        #   · **幂等**：重跑直接覆盖，不会因残留旧桶而重复；
        #   · **自愈**：源数据修正后重跑即修复脏桶（DELETE 方案下旧桶会一直留着）。
        # 代价：无法删除"源里已消失"的桶。对分钟线这种只增不减的源，
        # 该情形只可能来自人工清理 minute_bar，可接受（且有 131 行质量标记兜底）。
        sql = _INSERT_HEAD.format(t=t, iv=iv) + \
              "WHERE ts >= :since\nGROUP BY symbol, bucket\n" + _UPSERT_TAIL
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
            # ★ 2026-10-08 修（PR review I5）：重试成功时 break，但 `lags` 仍留着
            #   第 1 轮的失败值→ 末尾 `if lags:` 会打一条 ERROR 级「重试后仍未追平」，
            #   而数据其实已正确。这类假告警会稀释真告警的信噪比。
            lags = {}
            break
        lags = {t: f"期望≥{e} 实际{g}" for t, (e, g) in pending.items()}
        logger.warning(
            f"[synth] 校验未通过（第{attempt}/{max_retries}次重试）：{lags}；"
            f"疑似 minute_bar 正在被 MinuteCollector 并发写入")
        if attempt == max_retries:
            break
        # 重跑：扩大窗口重算，覆盖刚被并发写入的分钟线（纯 upsert，无需先 DELETE）
        since = datetime.now(_SH_TZ) - timedelta(days=days_back)
        for t, iv in _BARS:
            sql = _INSERT_HEAD.format(t=t, iv=iv) + \
                  "WHERE ts >= :since\nGROUP BY symbol, bucket\n" + _UPSERT_TAIL
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
           "minute_pipeline_lock", "LOCK_MINUTE_PIPELINE",
    "advisory_lock_holder", "release_stale_pipeline_lock"]
