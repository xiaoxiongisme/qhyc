# -*- coding: utf-8 -*-
"""G4 · 期货官方交易日历（唯一权威入口，替代股票日历 tool_trade_date_hist_sina）。

tool_trade_date_hist_sina 是 A 股日历，与期货作息不同（中金所 09:15 开盘、股指/国债无夜盘、
节前各所提前收盘、夜盘时段各所不一），套用会系统性错并污染护栏与门禁的时间边界。

真源 = futures_rule 表的 distinct trade_date（迁移 026 + seed_futures_rule.py 播种，
来源 akshare futures_rule(date=)）。该表「有数据的日期即交易日」，天然是期货专属日历。

覆盖边界 fail-loud：超出已播种区间时不假装知道——strict=True 抛错，否则告警后退化为
「周一~周五」。扩覆盖：seed_futures_rule.py --start 2015-01-01（可续跑）。
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
_cache: dict = {"ts": 0.0, "days": None, "min": None, "max": None}
_warned: set = set()


class CalendarCoverageError(RuntimeError):
    """日期超出 futures_rule 已播种覆盖范围（fail-loud）。"""


def _load():
    with _lock:
        if _cache["days"] is not None and (_time.time() - float(_cache["ts"])) < _TTL:
            return _cache["days"], _cache["min"], _cache["max"]
    with session_scope() as s:
        rows = s.execute(text("SELECT DISTINCT trade_date FROM futures_rule")).fetchall()
    days = {r[0] for r in rows if r[0] is not None}
    if not days:
        logger.warning(
            "[calendar] futures_rule 无数据，G4 官方日历不可用；"
            "请先跑 scripts/seed_futures_rule.py（勿退回股票日历）")
    lo, hi = (min(days), max(days)) if days else (None, None)
    with _lock:
        _cache.update({"ts": _time.time(), "days": days, "min": lo, "max": hi})
    return days, lo, hi


def calendar_coverage():
    """已覆盖区间 (min_date, max_date)；无数据时 (None, None)。"""
    _, lo, hi = _load()
    return lo, hi


def futures_trading_days(start=None, end=None) -> list:
    """[start, end] 区间内期货交易日（升序）。区间在覆盖外返回空列表（不用股票日历补齐）。"""
    days, _, _ = _load()
    if start is None:
        return sorted(days)
    if end is None:
        end = _dt.date.max
    return sorted(d for d in days if start <= d <= end)


def is_futures_trading_day(d, *, strict: bool = False) -> bool:
    """是否期货交易日（G4 官方源）。覆盖外：strict 抛错，否则告警后退化为周一~周五。"""
    d = _dt.date.fromisoformat(str(d)[:10])
    days, lo, hi = _load()

    if not days:
        if strict:
            raise CalendarCoverageError("[fail-loud] futures_rule 无数据，无法判定期货交易日")
        if str(d) not in _warned:
            _warned.add(str(d))
            logger.warning(f"[calendar] futures_rule 无数据，{d} 退化为「周一~周五」（G4 缺口）")
        return d.weekday() < 5

    if d < lo or d > hi:
        if strict:
            raise CalendarCoverageError(
                f"[fail-loud] {d} 超出 futures_rule 覆盖区间 [{lo}, {hi}]，拒绝猜测")
        if str(d) not in _warned:
            _warned.add(str(d))
            logger.warning(
                f"[calendar] {d} 超出 futures_rule 覆盖 [{lo}, {hi}]，退化为「周一~周五」（G4 缺口）")
        return d.weekday() < 5

    return d in days
