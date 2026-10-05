# -*- coding: utf-8 -*-
"""G4 · 期货官方交易日历（唯一权威入口，替代股票日历 tool_trade_date_hist_sina）。

tool_trade_date_hist_sina 是 A 股日历，与期货作息不同（中金所 09:15 开盘、股指/国债无夜盘、
节前各所提前收盘、夜盘时段各所不一），套用会系统性错并污染护栏与门禁的时间边界。

真源 = futures_rule 表的 distinct trade_date（迁移 026 + seed_futures_rule.py 播种，
来源 akshare futures_rule(date=)）。该表「有数据的日期即交易日」，天然是期货专属日历。

覆盖边界 fail-loud：表只记录**交易日**，故「无行」二义（休市 or 未来未播种）。本模块配合
`cfg_calendar_seed_meta.seeded_through`（由 seed_futures_rule.py 回写，迁移 031 建表）做三分：
有行=交易日；无行且<=截止日=**确定的非交易日**；无行且>截止日=真未知（strict 抛错/否则告警）。
扩覆盖：seed_futures_rule.py --start 2015-01-01（可续跑，续跑后会刷新截止日）。
"""
from __future__ import annotations

import datetime as _dt
import threading
import time as _time

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import logger

_TTL = 3600.0
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "days": None, "min": None, "seeded_through": None}
_warned: set = set()


class CalendarCoverageError(RuntimeError):
    """日期超出已播种范围且无法确认（fail-loud，绝不用「周一~周五」静默兜底）。"""


def _load():
    """返回 (交易日集合, 最早交易日, 已播种截止日)。

    ⚠ `futures_rule` **只记交易日**，故「无行」二义（休市 or 未来未播种）。必须配合
    `cfg_calendar_seed_meta.seeded_through`（播种脚本回写）才能区分，见 is_futures_trading_day。
    """
    with _lock:
        if _cache["days"] is not None and (_time.time() - float(_cache["ts"])) < _TTL:
            return _cache["days"], _cache["min"], _cache["seeded_through"]
    with session_scope() as s:
        rows = s.execute(text("SELECT DISTINCT trade_date FROM futures_rule")).fetchall()
        try:
            st = s.execute(
                text("SELECT value FROM cfg_calendar_seed_meta WHERE key='seeded_through'")
            ).scalar()
        except Exception:  # noqa: BLE001 —— 元数据表可能尚未应用(031)
            st = None
    days = {r[0] for r in rows if r[0] is not None}
    seeded_through = _dt.date.fromisoformat(str(st)[:10]) if st else None
    if not days:
        logger.warning(
            "[calendar] futures_rule 无数据，G4 官方日历不可用；"
            "请先跑 scripts/seed_futures_rule.py（勿退回股票日历）")
    elif seeded_through is None:
        logger.warning(
            "[calendar] cfg_calendar_seed_meta.seeded_through 缺失，无法区分"
            "「已确认休市」与「未来未播种」；请重跑 seed_futures_rule.py 或应用迁移 031")
    lo = min(days) if days else None
    with _lock:
        _cache.update({"ts": _time.time(), "days": days, "min": lo,
                       "seeded_through": seeded_through})
    return days, lo, seeded_through


def calendar_coverage():
    """返回 (最早交易日, 已播种截止日)。无数据时 (None, None)。

    「已播种截止日」才是真正的判定边界：<= 它的「无行」= 确定的非交易日（休市/周末）。
    """
    _, lo, st = _load()
    return lo, st


def futures_trading_days(start=None, end=None) -> list:
    """[start, end] 区间内期货交易日（升序）。区间在覆盖外返回空列表（不用股票日历补齐）。"""
    days, _, _ = _load()
    if start is None:
        return sorted(days)
    if end is None:
        end = _dt.date.max
    return sorted(d for d in days if start <= d <= end)


def is_futures_trading_day(d, *, strict: bool = False) -> bool:
    """是否期货交易日（G4 官方源 futures_rule）。

    三分判定（缺行不再二义）：
      ① 表内有该日                      → True  （权威交易日）
      ② 表内无该日 且 <= seeded_through → False （**确定的非交易日**：休市/周末）
      ③ 表内无该日 且 >  seeded_through → 未知：strict=True 抛错；否则告警后退化为「周一~周五」

    ② 是本模块的关键：没有它，国庆休市（表止于节前最后交易日）会被当成"未知"，
    退化成「周一~周五」后把 10-01（周四·休市）**误判为交易日**——静默错且不报错。
    """
    d = _dt.date.fromisoformat(str(d)[:10])
    days, lo, seeded_through = _load()

    if not days:
        if strict:
            raise CalendarCoverageError("[fail-loud] futures_rule 无数据，无法判定期货交易日")
        if str(d) not in _warned:
            _warned.add(str(d))
            logger.warning(f"[calendar] futures_rule 无数据，{d} 退化为「周一~周五」（G4 缺口）")
        return d.weekday() < 5

    if d in days:
        return True

    # ② 已播种范围内无记录 = 确定的非交易日（休市/周末），无需任何兜底
    if seeded_through is not None and d <= seeded_through:
        return False

    # ③ 真正的未来/未播种：拒绝猜测
    if strict:
        raise CalendarCoverageError(
            f"[fail-loud] {d} 超出已播种截止日 {seeded_through}，无法确认（拒绝猜测）")
    if str(d) not in _warned:
        _warned.add(str(d))
        logger.warning(
            f"[calendar] {d} 超出已播种截止日 {seeded_through}，退化为「周一~周五」（G4 缺口）")
    return d.weekday() < 5
