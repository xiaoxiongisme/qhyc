# -*- coding: utf-8 -*-
"""L0 分钟写入护栏（CL-20261112-09，2026-11-12 23:20 用户拍板）。

背景（22:46 实证，纠正上一版措辞）
----------------------------------
``akshare.futures_zh_minute_sina(period="1")`` 固定返回最近约 1024 根分钟，
``--data-length`` / ``datalen`` 参数被服务端忽略。所谓「实时采集」每轮都在
**重放最近数个交易日的全部历史分钟**，靠 ``ON CONFLICT (symbol, ts) DO NOTHING``
幂等去重。主键不含 ``src``：先到的历史源占住键之后，实时链路每轮重放都会撞上它。
078 的 BEFORE INSERT 触发器 22:00 上线后数分钟即捕获 309 行
``attempted=akshare_1min / kept=akshare_1min_nov2026``（ts 全在 11-06 10:50~11:30）
与 28 行 contract 层历史 CSV 冲突。这些**不是新行情被覆盖**，而是「重放历史键」
被静默丢弃；但每轮重试会永久刷 ``last_seen`` / ``n_attempts``，形成噪音。

两件护栏
--------
1. :func:`tail_bars` —— **本地裁切**接口返回的窗口（服务端无法少返回），实时链路
   只提交最近 N 根。默认 120 而非 30：``minute_and_bars`` 每 30 分钟一轮、
   ``cron_fetch_1min`` 每 15 分钟一轮，30 根只够 1 轮容错；120 根 ≈ 4 轮容错，
   再长的缺口交给日终作业。可用 ``QHYC_L0_REALTIME_BARS`` 或
   ``ingest.l0_realtime_bars`` 调整。
2. :func:`filter_cross_src_occupied` —— 写库前批量查候选键，剔除已被**其它**
   ``src`` 占据的键，让 keep-first 从「靠 DO NOTHING 隐式吞掉、每轮反复重试」
   变成显式、可计数、可被守卫验证的行为。

刻意不做的事
------------
* **不改成 DO UPDATE**：用户拍板 keep-first。任何覆盖都会让历史批次改写实时值。
* **不覆盖已有值**：日终作业只补缺失键，异常值只报告，不改写任何一行。
* **不造交易日历**：窗口判定用 weekday 兜底；节假日多跑一轮无正确性风险
  （与 ``app/ingest/trading_window.py`` 同一取舍）。

``ops.l0_ingest_audit`` / ``ops.l0_eod_check`` 由 migrations/079 建立，守卫据此
判断「护栏真在跑」，而不是「表建好了没人读」。
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

_SH_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_REALTIME_BARS = 120
ENV_REALTIME_BARS = "QHYC_L0_REALTIME_BARS"
MAX_REALTIME_BARS = 1024

#: 精确键匹配：只查候选 (symbol, ts)，避免范围查询把整个 hypertable 拉进结果集。
#: 分块执行，单块默认 2000 键（4000 个绑定参数，远低于 65535 上限）。
# ⚠ 必须用 CAST(...)，不能写 ``:syms::text[]``：SQLAlchemy text() 会把 ``::``
# 与参数冒号一起吞掉，实测原样发给 psycopg 后报 ``syntax error at or near ":"``。
# ⚠ ``m.ts`` 的上下界谓词不可省：minute_bar 是 281 个 chunk 的 hypertable，
# 没有它 planner 会对每个 chunk 做 nested loop，实测 2000 键查询直接超时（SIGTERM）；
# 加上后 TimescaleDB 能裁剪 chunk，同一探针 0.06s / 210 行。
_OCCUPIED_SQL = text(
    "SELECT k.symbol, k.ts "
    "FROM unnest(CAST(:syms AS text[]), CAST(:tss AS timestamptz[])) "
    "     AS k(symbol, ts) "
    "JOIN l0_raw.minute_bar m ON m.symbol = k.symbol AND m.ts = k.ts "
    "WHERE m.src IS DISTINCT FROM CAST(:src AS text) "
    "  AND m.ts >= CAST(:lo AS timestamptz) "
    "  AND m.ts <= CAST(:hi AS timestamptz)"
)

_AUDIT_INSERT = text(
    "INSERT INTO ops.l0_ingest_audit "
    " (chain, n_symbols, n_candidates, n_written, n_skipped_cross_src, "
    "  max_lookback_bars, min_ts, max_ts, extra) "
    "VALUES (:chain, :n_symbols, :n_candidates, :n_written, "
    "        :n_skipped_cross_src, :max_lookback_bars, :min_ts, :max_ts, "
    "        CAST(:extra AS jsonb))"
)


def realtime_bars_limit() -> int:
    """实时链路保留的最近分钟根数。

    优先级：环境变量 > ``ingest.l0_realtime_bars`` > 默认 120。
    配置字段有默认值，故**不需要改被 .gitignore 排除的 config/cloud.yaml**。
    """
    raw: Any = os.getenv(ENV_REALTIME_BARS)
    if raw is None:
        try:
            from app.core.config import get_settings

            raw = getattr(
                get_settings().yaml.ingest, "l0_realtime_bars", DEFAULT_REALTIME_BARS
            )
        except Exception:  # noqa: BLE001 —— 配置不可用时退回默认值
            raw = DEFAULT_REALTIME_BARS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_REALTIME_BARS
    return max(1, min(n, MAX_REALTIME_BARS))


def tail_bars(rows: list[dict], n: int | None) -> list[dict]:
    """按 ts 升序取最后 ``n`` 根；``n`` 为 ``None`` 或 <=0 表示保留完整窗口（日终作业用）。

    接口返回按时间升序，但不同品种/异常返回并不保证有序，故显式排序。
    """
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: r["ts"])
    if n is None or n <= 0 or len(ordered) <= n:
        return ordered
    return ordered[-n:]


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def filter_cross_src_occupied(
    session: Session,
    rows: list[dict],
    src: str,
    chunk_size: int = 2000,
) -> tuple[list[dict], int, bool]:
    """剔除已被其它 ``src`` 占据的 ``(symbol, ts)`` 键。

    返回 ``(kept, n_skipped, guard_failed)``。查询异常时 **fail-open**：原样返回
    候选行、靠原有 ``ON CONFLICT DO NOTHING`` 兜底（不阻断行情采集），但置
    ``guard_failed=True`` 让审计与守卫看得见——绝不静默降级。
    """
    if not rows:
        return [], 0, False
    occupied: set[tuple[str, datetime]] = set()
    items = [(str(r["symbol"]), _utc(r["ts"])) for r in rows]
    for i in range(0, len(items), max(1, chunk_size)):
        chunk = items[i : i + max(1, chunk_size)]
        try:
            found = session.execute(
                _OCCUPIED_SQL,
                {
                    "syms": [c[0] for c in chunk],
                    "tss": [c[1] for c in chunk],
                    "src": src,
                    "lo": min(c[1] for c in chunk),
                    "hi": max(c[1] for c in chunk),
                },
            ).all()
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"[l0_guard] 跨源占位查询失败，本轮退回 DO NOTHING 旧行为: "
                f"{type(e).__name__}: {e}"
            )
            return list(rows), 0, True
        occupied.update((str(a), _utc(b)) for a, b in found)

    kept = [
        r for r in rows
        if (str(r["symbol"]), _utc(r["ts"])) not in occupied
    ]
    return kept, len(rows) - len(kept), False


def record_ingest_audit(
    chain: str,
    stats: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> bool:
    """写一条采集审计（独立连接/独立事务，绝不污染主写入事务）。

    审计设施失败只记日志，不影响行情落库。
    """
    import json

    try:
        from app.core.db import get_engine

        with get_engine().begin() as conn:
            conn.execute(
                _AUDIT_INSERT,
                {
                    "chain": chain,
                    "n_symbols": int(stats.get("n_symbols") or 0),
                    "n_candidates": int(stats.get("n_candidates") or 0),
                    "n_written": int(stats.get("n_written") or 0),
                    "n_skipped_cross_src": int(
                        stats.get("n_skipped_cross_src") or 0
                    ),
                    "max_lookback_bars": int(stats.get("max_lookback_bars") or 0),
                    "min_ts": stats.get("min_ts"),
                    "max_ts": stats.get("max_ts"),
                    "extra": json.dumps(extra or {}, ensure_ascii=False, default=str),
                },
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"[l0_guard] 写 ops.l0_ingest_audit 失败（不影响采集）: "
            f"{type(e).__name__}: {e}"
        )
        return False


def trading_session_bounds(
    now: datetime | None = None,
) -> tuple[datetime, datetime, date] | None:
    """当前期货交易日的分钟窗口 ``(start, end, trade_date)``。

    中国期货一个交易日 = **上一工作日夜盘 21:00 → 当日日盘 15:00**。周一交易日
    的夜盘是上周五 21:00（周五夜盘延续到周六凌晨），故窗口起点必须回退到
    上一个工作日，不能简单取「昨天 21:00」。

    ``end`` 固定当日 15:20（日盘收盘 +20 分钟补尾，与 trading_window.DAY_END 一致）。
    周六/周日返回 ``None``：15:20 档在周末直接跳过，不重复检查周五交易日。
    """
    n = (now or datetime.now(_SH_TZ)).astimezone(_SH_TZ)
    if n.weekday() >= 5:
        return None
    prev = n.date() - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    start = datetime.combine(prev, time(20, 50), tzinfo=_SH_TZ)
    end = datetime.combine(n.date(), time(15, 20), tzinfo=_SH_TZ)
    return start, end, n.date()


def bars_span(rows: Iterable[dict]) -> int:
    """候选行里单品种最大根数（用于审计「实时窗口确实被裁切」）。"""
    per_symbol: dict[str, int] = {}
    for r in rows:
        key = str(r.get("symbol"))
        per_symbol[key] = per_symbol.get(key, 0) + 1
    return max(per_symbol.values(), default=0)


__all__ = [
    "DEFAULT_REALTIME_BARS",
    "bars_span",
    "filter_cross_src_occupied",
    "realtime_bars_limit",
    "record_ingest_audit",
    "tail_bars",
    "trading_session_bounds",
]
