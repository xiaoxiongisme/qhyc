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
from app.data.tradable_scope import (
    Scope,
    load_scope,
    log_scope,
    scope_params,
    where_clause,
)

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

# =============================================================================
# ★ 2026-10-31 重大口径修正（用户反馈：同花顺小时 K 一天 ≥6 根）
# -----------------------------------------------------------------------------
# 原实现用 ``time_bucket(INTERVAL '60 minutes', ts)`` —— 按**自然时间整点**切桶。
# 于是 SC888 一天得到 09:00 10:00 11:00 13:00 14:00 15:00 21:00 22:00 23:00
# 这种"整点网格"，**每个交易小节末尾不足 60 分钟的那一段被吞掉**，且每个小节
# 的 bar 起点被强行对齐到整点，与实际成交节奏脱节。
#
# 正确口径（同花顺/文华/博易主流，用户给的 SC 参照：
#   `10:00 11:15 14:15 15:00 22:00 23:00`，RB888 实测同样 6 根）反推出的规则：
#   1. 把分钟按 ts 升序串起来，**跳过小节间休市继续累计**（只看有没有分钟数据）；
#   2. 相邻分钟间隔 > ``_SESSION_MAX_GAP_MIN`` 分钟才切断——商品最长午休 120 分钟
#      不被切断，日盘↔夜盘的 360 分钟必然切断（所以夜盘不会并进日盘）；
#   3. 每满 ``step`` 个交易分钟成一根 bar，当前段剩余不足部分单独成一根；
#   4. bar 时间标签 = 该 bar 最后一根分钟的 ts，再按该源的「分钟时间戳约定」补偏移：
#        kqm_1m / tqsdk_1min 记分钟【起点】(09:00…14:59) → +1 分钟才是收边界
#        akshare_1min / wh9999_1m 记分钟【终点】(09:01…15:00) → 本身就是收边界
#      判定依据：本连续段首根分钟的分钟数能否被 30 整除（整点/半点 = 起点约定）。
#
# ⚠ 2026-10-31 起 _BARS 四张表全部走这条规则（30m/60m 自 2026-10-31 引入，
#   5m/15m 同日补齐——此前 5m/15m 用 time_bucket 自然整点桶，会把全量重建
#   修好的网格在下次增量时改回 13:30/10:30 等错误标签）。
#   网格定义见 ``_SESSION_DAY_BOUNDS``，须与 rebuild_bars_session.py 保持一致。
# =============================================================================
_SESSION_MAX_GAP_MIN = 150

#: 多源分钟去重优先级（同一 (symbol, ts) 被多个 src 写入时取优先级最高的）
# 2026-11-07 P0-1：登记 10-30 换源后的新实时源。
# 该源 ts 记为【终点】(首根 09:01 / 末根 15:00)，实测覆盖 2026-10-30 21:01~11-06 11:30。
# 未登记时 _session_insert_sql 不会减 1 分钟归一，导致四张 bar_* 全部系统性少聚合 1 根。
_SESSION_SRC_PRIORITY = ["kqm_1m", "wh9999_1m", "tqsdk_1min", "akshare_1min",
                         "akshare_1min_nov2026"]

#: 「分钟时间戳约定」登记：这批源把 ts 记为该分钟的【终点】(HH:01…HH:00)，
#: 需要先减 1 分钟归一到【起点】约定，再统一落边界。
#:   kqm_1m     first=09:00 last=14:59  → 起点（已验证 ZN888 2025-06-03）
#:   tqsdk_1min first=09:00 last=14:59  → 起点（已验证 ZN888 2026-09-24）
#:   akshare_1min first=09:01 last=15:00 → 终点（已验证 ZN888/SC888 2026-10-27）
#:   wh9999_1m first=09:01 last=15:00    → 终点（已验证 A888 2015-01-05）
#: ⚠ 若将来接入新的分钟源，务必先到这里登记它的约定，否则整段 bar 会偏 1 分钟。
_SESSION_END_LABELED_SRCS = ["akshare_1min", "wh9999_1m",
                             "akshare_1min_nov2026"]

#: 已知但**不参与当前增量窗口**的历史源。仅用于 fail-loud 白名单，
#: 不加入 _SESSION_END_LABELED_SRCS —— 其时间戳约定尚未验证（见报告 M19），
#: 贸然归一会改动 2026 全年 500 万行历史 L1，风险高于收益。
_KNOWN_HISTORICAL_SRCS = frozenset({"local_csv_20261030"})

#: 日盘固定边界（相对 09:00 的分钟偏移）：跳过 10:15-10:30 与 11:30-13:30 两段休市。
#:   60m → 10:00 / 11:15 / 14:15 / 15:00
#:   30m → 09:30 / 10:00 / 10:45 / 11:15 / 13:45 / 14:15 / 14:45 / 15:00
#:   15m → 晨 09:15..10:15(每15) → 10:45..11:30；午 13:45..15:00(每15)
#:   5m  → 晨 09:05..10:15(每5)  → 10:35..11:30；午 13:35..15:00(每5)
#: ⚠ 必须与 ``scripts/validate/rebuild_bars_session.py::DAY_BOUNDS`` 逐位相同，
#:   否则「增量合成」与「全量重建」会给出互相矛盾的 bar（历史上 30m 曾遗漏
#:   14:45、5m/15m 曾整体退回自然整点桶，均由此处不同步引起）。
#:   校验脚本：``scripts/validate/check_bar_grid_consistency.py``。
_SESSION_DAY_BOUNDS = {
    60: [60, 135, 315, 360],
    30: [30, 60, 105, 135, 285, 315, 345, 360],
    15: [15, 30, 45, 60, 75, 105, 120, 135, 150,
         285, 300, 315, 330, 345, 360],
    5:  [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75,
         95, 100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150,
         275, 280, 285, 290, 295, 300, 305, 310, 315, 320, 325, 330,
         335, 340, 345, 350, 355, 360],
}


def _session_insert_sql(step: int, where: str, tail: str, t: str = "") -> str:
    """按「交易所固定交易时段网格」聚合（见 rebuild_bars_session._build_sql 同源逻辑）。

    日盘从 09:00 起、跳过两段休市，边界固定在墙上；夜盘从 21:00 起（无休市），
    残段截断到 seg 自然收盘。锚定固定墙钟（09:00/21:00 on seg_date），
    缺失分钟只让某根 bar 少 1 分钟，标签不变 —— 根治旧 row_number 法对缺失分钟的脆弱。
    """
    bounds = _SESSION_DAY_BOUNDS[step]
    day_when = " ".join(f"WHEN rel_end <= {b} THEN {b}" for b in bounds)
    day_case = f"CASE {day_when} ELSE {bounds[-1]} END"
    return (
        "WITH m AS (\n"
        "    SELECT symbol, ts, open, high, low, close, volume, amount, open_interest, src\n"
        "    FROM minute_bar\n"
        f"    {where}\n"
        "), norm AS (\n"
        "    SELECT *, ts - CASE WHEN src = ANY(:end_srcs)\n"
        "                        THEN interval '1 minute' ELSE interval '0 minute' END AS ts_n\n"
        "    FROM m\n"
        "), dedup AS (\n"
        "    -- 去重必须在「归一化之后」按 ts_n 做：不同分钟戳约定的源（tqsdk_1min 起点\n"
        "    -- 约定末根 14:59 与 akshare_1min 终点约定末根 15:00）归一后同为 ts_n=14:59，\n"
        "    -- 若按原始 ts 去重则两者都保留 → 量额双倍、脏值混进 high/low、\n"
        "    -- array_agg(close ORDER BY ts_n DESC)[1] 在并列时顺序未定义使收盘价不确定。\n"
        "    -- 详见 scripts/validate/rebuild_bars_session.py 同名注释。\n"
        "    SELECT DISTINCT ON (symbol, ts_n) * FROM norm\n"
        "    ORDER BY symbol, ts_n, array_position(:srcs, src)\n"
        "), gapd AS (\n"
        "    SELECT *, CASE WHEN prev IS NULL\n"
        "                     OR extract(epoch FROM (ts_n - prev)) / 60 > :maxgap\n"
        "                THEN 1 ELSE 0 END AS brk\n"
        "    FROM (SELECT *, lag(ts_n) OVER (PARTITION BY symbol ORDER BY ts_n) prev FROM dedup) g\n"
        "), segs AS (\n"
        "    SELECT *, sum(brk) OVER (PARTITION BY symbol ORDER BY ts_n) AS grp FROM gapd\n"
        "), segmeta AS (\n"
        "    SELECT *, min(ts_n) OVER (PARTITION BY symbol, grp) AS seg_first,\n"
        "             max(ts_n) OVER (PARTITION BY symbol, grp) AS seg_last\n"
        "    FROM segs\n"
        "), typed AS (\n"
        "    SELECT *, (seg_first AT TIME ZONE 'Asia/Shanghai')::date AS seg_date,\n"
        "           bool_or(EXTRACT(hour FROM ts_n AT TIME ZONE 'Asia/Shanghai') "
        "BETWEEN 9 AND 14)\n"
        "               OVER (PARTITION BY symbol, grp) AS is_day\n"
        "    FROM segmeta\n"
        "), bucketed AS (\n"
        "    SELECT symbol, open, high, low, close, volume, amount, open_interest, ts_n, is_day,\n"
        "           CASE\n"
        "             WHEN is_day THEN\n"
        "               (seg_date + TIME '09:00') AT TIME ZONE 'Asia/Shanghai'\n"
        f"                 + ( {day_case} ) * interval '1 minute'\n"
        "             ELSE\n"
        "               LEAST(\n"
        "                 (seg_date + TIME '21:00') AT TIME ZONE 'Asia/Shanghai'\n"
        f"                   + ( floor( EXTRACT(epoch FROM (ts_n\n"
        "                       - ((seg_date + TIME '21:00') AT TIME ZONE 'Asia/Shanghai')) )"
        f" / 60 / {step} ) + 1 ) * ({step} * interval '1 minute'),\n"
        "                 seg_last + interval '1 minute'\n"
        "               )\n"
        "           END AS bucket_end\n"
        "    FROM (\n"
        "        SELECT *, EXTRACT(epoch FROM ((ts_n + interval '1 minute')\n"
        "                      - ((seg_date + TIME '09:00') AT TIME ZONE 'Asia/Shanghai')))/60 "
        "AS rel_end\n"
        "        FROM typed\n"
        "    ) t\n"
        ")\n"
        "INSERT INTO {t} (symbol, bucket, open, high, low, close, volume, amount, open_interest)\n"
        "SELECT symbol, bucket_end AS bucket,\n"
        "       (array_agg(open  ORDER BY ts_n))[1],\n"
        "       max(high), min(low),\n"
        "       (array_agg(close ORDER BY ts_n DESC))[1],\n"
        "       sum(volume)::bigint, sum(amount),\n"
        "       (array_agg(open_interest ORDER BY ts_n DESC))[1]\n"
        "FROM bucketed\n"
        "GROUP BY symbol, bucket_end\n"
        f"{tail}"
    ).format(t=t)


#: 会话式切分对全部 bar_* 表生效（2026-10-31 起）。
#: 此前只有 30m/60m 走会话式，5m/15m 仍走 ``time_bucket`` 自然整点桶，于是
#: 全量重建把 5m/15m 修成同花顺网格后，**下一次增量又会用整点桶把它改回去**
#: （13:30 取代 13:35/13:45、10:30 取代 10:35/10:45），属静默双向拉锯。
#: 现四条频次统一走会话式，与 rebuild_bars_session.py 同源。
_SESSION_BARS: dict[str, int] = {
    "bar_5m": 5, "bar_15m": 15, "bar_30m": 30, "bar_60m": 60,
}

#: 兜底断言：``_BARS`` 里任何一张表若没在上登记，就会静默退回自然整点桶。
#: 宁可在 import 期炸掉，也不要让错误的 bar 悄悄落库。
_MISSING = [t for t, _ in _BARS if t not in _SESSION_BARS]
if _MISSING:  # pragma: no cover - 配置即瘫痪
    raise RuntimeError(
        f"synthesizer: 以下 bar 表未登记会话式切分，会退回自然整点桶: {_MISSING}")

_INSERT_HEAD = (
    "INSERT INTO {t} (symbol, bucket, open, high, low, close, volume, amount, open_interest)\n"
    "SELECT symbol,\n"
    "       time_bucket(INTERVAL '{iv}', ts, 'Asia/Shanghai') AS bucket,\n"
    "       first(open, ts), max(high), min(low), last(close, ts),\n"
    "       sum(volume)::bigint, sum(amount), last(open_interest, ts)\n"
    "FROM minute_bar\n"
)
#: ⚠ 已停用（2026-10-31）：自然整点桶口径与同花顺网格互斥，5m/15m 一旦走到
#: 这条分支就会产出 13:30/10:30 等错误标签。保留仅为追溯，勿重新启用。

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


def assert_registered_srcs(session: Session, lookback_days: int = 7) -> list[str]:
    """P0-1 防复发硬断言：近 N 天 continuous 层出现的 src 必须全部登记。

    这次故障的根因不是算法错，而是「新源静默进入、无人登记」。故在任何合成
    动作之前先查库，发现未登记 src 直接抛错（fail loud），不许静默产出错位 bar。
    白名单 = 去重优先级 ∪ 终点标签源 ∪ 已知历史源。
    """
    rows = session.execute(text(
        "SELECT DISTINCT src FROM l0_raw.minute_bar "
        "WHERE layer = 'continuous' AND src IS NOT NULL "
        "  AND ts >= now() - (:d || ' days')::interval"
    ), {"d": lookback_days}).scalars().all()
    known = (set(_SESSION_SRC_PRIORITY) | set(_SESSION_END_LABELED_SRCS)
             | set(_KNOWN_HISTORICAL_SRCS))
    unknown = sorted(set(rows) - known)
    if unknown:
        raise RuntimeError(
            f"[synth] 检测到未登记的分钟源 {unknown}；请先在 "
            f"_SESSION_SRC_PRIORITY / _SESSION_END_LABELED_SRCS 登记其时间戳约定"
            f"（起点 or 终点）后再合成，拒绝静默产出错位 bar")
    return sorted(set(rows))


def _max_minute_ts(session: Session) -> datetime | None:
    return session.execute(text("SELECT max(ts) FROM minute_bar")).scalar()


def _synth_one(session: Session, t: str, iv: str, since: datetime | None,
               tail: str, scope: Scope | None = None) -> int:
    """单表合成：自动在「自然整点桶」与「交易分钟会话桶」之间选择正确口径。

    ``scope``（CL-20261107-01 S2）：品种口径。``None`` / fail-open 时不过滤。
    过滤必须加在**读取侧**（``FROM minute_bar`` 的 WHERE），而不是落库后删除 ——
    后者会在 upsert 前短暂写入不该存在的品种，且与 ``purge`` 的删除边界打架。
    """
    step = _SESSION_BARS.get(t)
    # 「WHERE TRUE」打底：这样 since / 口径两个条件都能无条件以 AND 追加，
    # 不必为四种组合各写一套拼接（写死条件顺序是这类 SQL 拼装 bug 的高发点）。
    where = "WHERE TRUE"
    if since is not None:
        where += " AND ts >= :since"
    where += where_clause(scope, "symbol")
    params: dict = {}
    if since is not None:
        params["since"] = since
    if scope is not None:
        params.update(scope_params(scope))

    if step is None:
        sql = _INSERT_HEAD.format(t=t, iv=iv) + where + "\nGROUP BY symbol, bucket\n" + tail
        return _run(session, sql, params)
    sql = _session_insert_sql(step, where=where, tail=tail, t=t)
    params.update({
        "srcs": _SESSION_SRC_PRIORITY,
        "end_srcs": _SESSION_END_LABELED_SRCS,
        "maxgap": _SESSION_MAX_GAP_MIN,
    })
    return _run(session, sql, params)


def _session_floor(dt: datetime) -> datetime:
    """回退到「不晚于 dt 的最近一个会话起点」（只有 09:00 / 21:00 两类，取 09:00）。

    为什么需要它（2026-11-03，docs §36.12）
    --------------------------------------
    ``purge`` 的删除下界原本是「now − N 天」这种任意时刻（例如 14:19）。
    ``_synth_one`` 会按桶对齐把 **14:19 所在的整根 60m 桶删掉**，却只从 14:19
    之后读分钟 → 该桶被写成残缺值，且**不报错**。实测 10-30 14:15 那根 60m 的
    volume 只有正确聚合的零头（RB888 57,166 vs 124,953），由 C2 跨周期守卫抓到。

    会话起点只有 09:00（日盘）与 21:00（夜盘），夜盘最晚 02:30 收；故日盘 09:00
    必然落在两段行情之间，从 09:00 起算不会有任何块被截断。
    """
    local = dt.astimezone(_SH_TZ)
    cand = datetime(local.year, local.month, local.day, 9, 0, tzinfo=_SH_TZ)
    if cand > local:
        cand -= timedelta(days=1)
    return cand


def purge_recent_buckets(session: Session, days_back: int = 4,
                         scope: Scope | None = None) -> dict[str, int]:
    """删除「最近 days_back 天」的全部桶，供切换合成口径后清除旧网格残留。

    背景（2026-11-03，见 docs/V2样本外崩塌排查.md §36）
    -----------------------------------------------
    ``synthesize_bars_incremental`` 是**纯 upsert**（ON CONFLICT DO UPDATE），不删任何行。
    若库里已存在另一套网格的桶（例如存储过程用 ``time_bucket`` 写入的 09:00/11:00/13:00），
    单纯 upsert 只会**新增**会话网格的桶，两套网格并存（一天 9+6=15 根），
    下游按时间排序会把同一段行情数两遍。

    故换口径时必须先删后写，且**删除范围 ⊆ 重建范围**（否则删了不补 = 丢数据）。
    调用方应传 ``days_back >= synthesize_bars_incremental 的 days_back + 1``：
    后者内部取 ``since_session = _session_floor(now − (days_back+1) 天)``，而本函数
    取 ``_session_floor(now − days_back)``，两者相差恰好一天且都对齐到 09:00。

    安全边界：本函数只 DELETE **不提交**，由调用方与随后的 upsert 在同一事务提交；
    upsert 抛错则整体回滚，旧数据原样保留。
    """
    cutoff = datetime.now(_SH_TZ) - timedelta(days=days_back)
    # 会话表必须把下界对齐到会话起点：任意时刻作下界会把「下界所在的那根桶」
    # 一起删掉，而重建只从下界之后读分钟 → 半截桶（§36.12 实测踩过）。
    cutoff_session = _session_floor(cutoff)

    # ★ 2026-11-07（CL-20261107-01 S2）：删除范围必须与重建范围同口径。
    #   重建已按品种口径收口，若 purge 仍删「全部 symbol 的最近 N 天」，
    #   非白名单品种的历史会在每轮作业里被删掉一批却不再重建 ——
    #   那就从「停派生」悄悄变成了「分批删除」，与 D1「只导出不删」的决策
    #   直接冲突，且不可逆。所以这里跟着 scope 一起收窄。
    if scope is None:
        scope = load_scope(session)
    wc = where_clause(scope)
    extra = scope_params(scope)
    out: dict[str, int] = {}
    for t, _ in _BARS:
        c = cutoff_session if t in _SESSION_BARS else cutoff
        r = session.execute(
            text(f"DELETE FROM {t} WHERE bucket >= :c{wc}"), {"c": c, **extra})
        out[t] = r.rowcount
    log_scope(scope, f"purge {days_back}d")
    logger.info(
        f"[synth] purge 最近 {days_back} 天旧桶（换口径前置，下界 {cutoff_session:%Y-%m-%d %H:%M}）: {out}")
    return out


def synthesize_bars_incremental(session: Session, days_back: int = 3,
                                max_retries: int = 2,
                                scope: Scope | None = None) -> dict:
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
    # P0-1：先 fail-loud 校验源登记，避免「新源静默进入」再次发生。
    assert_registered_srcs(session, lookback_days=max(days_back + 2, 7))

    # CL-20261107-01 S2：品种口径。一次读取、全程复用 —— 若在循环里反复读，
    # 中途持仓变化会让不同频率落到不同口径上，产出彼此不一致的 bar_*。
    if scope is None:
        scope = load_scope(session)
    log_scope(scope, "incremental 合成")

    since = datetime.now(_SH_TZ) - timedelta(days=days_back)
    # ★ 会话式切分必须向前多看 1 天，并把起点**对齐到会话起点**：它按「连续分钟段」
    #   累计交易分钟计数，若 since 落在段的中间，前面的分钟没参与计数 → 该段所有 bar
    #   错位；若不对齐，还会把 since 所在的整根桶写成半截（§36.12）。单个连续段最长
    #   约 6 小时（夜盘 21:00→次日 02:30），多留 1 天余量足够。
    since_session = _session_floor(since - timedelta(days=1))
    stats: dict[str, int] = {}
    for t, iv in _BARS:
        stats[t] = _synth_one(
            session, t, iv,
            since_session if t in _SESSION_BARS else since,
            _UPSERT_TAIL, scope,
        )
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
        since_session = _session_floor(since - timedelta(days=1))
        for t, iv in _BARS:
            stats[t] = _synth_one(
                session, t, iv,
                since_session if t in _SESSION_BARS else since,
                _UPSERT_TAIL, scope,
            )
        session.commit()

    if lags:
        logger.error(f"[synth] 重试后仍未追平 minute_bar（可能 MinuteCollector 持续并发写入）: {lags}")
    else:
        logger.info("[synth] 校验通过：bar_* 已追平 minute_bar 最新桶")
    logger.info(f"[synth] incremental done: {stats}")
    return stats


def synthesize_bars_full(session: Session, work_mem: str = "2GB") -> dict:
    """全量重算（一次性迁移/回填）。耗时随分钟数据量线性增长。

    ★ 有意**不做**品种口径过滤（CL-20261107-01 S2 的唯一例外）。
      本函数会先 ``TRUNCATE`` 再重建；若只重建白名单品种，非白名单的
      149.6 万行历史就在 TRUNCATE 那一步**永久消失**，与 D1「只导出不删」
      的决策直接冲突。它是一次性回补/迁移工具，语义就是「全量」。
      需要按口径重建请用 ``synthesize_bars_incremental``（增量，带过滤）。
    """
    session.execute(text(f"SET LOCAL work_mem = '{work_mem}'"))
    stats: dict[str, int] = {}
    for t, _ in _BARS:
        session.execute(text(f"TRUNCATE {t}"))
    session.commit()
    for t, iv in _BARS:
        stats[t] = _synth_one(
            session, t, iv, None,
            "ON CONFLICT (symbol, bucket) DO NOTHING",
        )
    session.commit()
    logger.info(f"[synth] full done: {stats}")
    return stats


__all__ = ["synthesize_bars_incremental", "synthesize_bars_full",
           "minute_pipeline_lock", "LOCK_MINUTE_PIPELINE",
    "advisory_lock_holder", "release_stale_pipeline_lock"]
