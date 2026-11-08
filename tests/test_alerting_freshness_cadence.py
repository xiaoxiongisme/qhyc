# -*- coding: utf-8 -*-
"""新鲜度巡检的交易日/时间戳分类回归护栏（CL-20261109-02）。

背景：15 张表原先共用 ``(today - max).days > 3`` 的自然日判据。周末两天就把
自然日差顶到 3，国庆/春节长假则让**全部表同时越线**——2026-11-09 00:00 那条
「日线数据滞后 3 天（最新 2026-11-06）」就是这么来的（数据零滞后）。

这些测试不连数据库：用 ``StubSession`` 喂假的 ``futures_rule`` 交易日列表与各表
max 值，只验证 ``_check_freshness`` 的判定逻辑。锁住三件事：

  1. 周末/长假期间不误报（这是本次改动的目的）；
  2. 真滞后仍能报，且「当天缺当天报」的时点不早于结算入库时刻；
  3. 夜盘跨零点的桶（次日 00:00-03:00）算**前一交易日**，不算新数据。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "alerting_under_test", os.path.join(_REPO, "app", "ops", "alerting.py"))
alerting = importlib.util.module_from_spec(_spec)
# dataclass 需要模块已登记在 sys.modules（否则 @dataclass 取 __module__ 会 None）
sys.modules[_spec.name] = alerting
_spec.loader.exec_module(alerting)

CST = alerting.CST
D = dt.date

# 含国庆长假（09-30 → 10-08 断档 7 天）的交易日序列
TRADING_DAYS = [
    D(2026, 9, 28), D(2026, 9, 29), D(2026, 9, 30),
    D(2026, 10, 8), D(2026, 10, 9),
    D(2026, 11, 2), D(2026, 11, 3), D(2026, 11, 4), D(2026, 11, 5), D(2026, 11, 6),
]

SESSION_TABLES = [
    "hourly_bar", "minute_bar", "bar_60m",
    "l2_adj.bar_5m_ba", "l2_adj.bar_15m_ba",
    "l2_adj.bar_30m_ba", "l2_adj.bar_60m_ba",
]


class _Res:
    def __init__(self, v):
        self.v = v

    def scalars(self):
        return self

    def all(self):
        return self.v

    def scalar(self):
        return self.v


class StubSession:
    """只应答两类 SQL：futures_rule 的交易日列表、``SELECT max(col) FROM tbl``。"""

    def __init__(self, maxima, days=None):
        self.maxima = maxima
        self.days = TRADING_DAYS if days is None else days

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "futures_rule" in sql:
            return _Res(sorted(self.days))
        for tbl, val in self.maxima.items():
            if " FROM " + tbl in sql:
                return _Res(val)
        return _Res(None)


def _cfg(**kw):
    cfg = alerting.alert_config_from_env()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _run(now, maxima=None, days=None, cfg=None):
    cfg = cfg or _cfg()
    base = {t: D(2026, 11, 6) for t in cfg.freshness_tables}
    base.update(maxima or {})
    return alerting._check_freshness(StubSession(base, days), cfg, now=now)


def _keys(findings):
    return sorted(f["key"] for f in findings)


# --------------------------------------------------------------------------
# 1. 不误报：周末 / 长假 / 真实时刻
# --------------------------------------------------------------------------

def test_weekend_no_false_alarm():
    """周六 10:00，数据停在周五 → 零告警（原实现等周日 00:00 后就会报）。"""
    assert _keys(_run(dt.datetime(2026, 11, 7, 10, 0, tzinfo=CST))) == []


def test_holiday_first_morning_no_false_alarm():
    """国庆后首个交易日 09:30，数据仍停在 09-30 → 零告警。

    这正是 24h/3 天阈值时代的经典误报：8 个自然日，但只隔 0 个交易日。
    """
    stale = {t: D(2026, 9, 30) for t in _cfg().freshness_tables}
    assert _keys(_run(dt.datetime(2026, 10, 8, 9, 30, tzinfo=CST), stale)) == []


def test_holiday_after_settlement_still_reports():
    """但长假后 22:00（过了 21 点结算截止）仍缺当日 → 15 张表全报，不是静音。"""
    stale = {t: D(2026, 9, 30) for t in _cfg().freshness_tables}
    found = _run(dt.datetime(2026, 10, 8, 22, 0, tzinfo=CST), stale)
    assert len(found) == len(stale), _keys(found)


def test_current_moment_clean():
    """2026-11-09 00:09（真实时刻，数据在 11-06）→ 零告警。"""
    assert _keys(_run(dt.datetime(2026, 11, 9, 0, 9, tzinfo=CST))) == []


# --------------------------------------------------------------------------
# 2. 真滞后必须抓得到
# --------------------------------------------------------------------------

def test_real_lag_reports_trading_day_gap():
    """daily_bar 停在 11-03 → 缺 3 个交易日，消息里带交易日口径而非自然日。"""
    found = _run(dt.datetime(2026, 11, 9, 9, 0, tzinfo=CST),
                 {"daily_bar": D(2026, 11, 3)})
    assert _keys(found) == ["fresh_daily_bar"]
    assert "缺 3 个交易日" in found[0]["msg"]


def test_same_day_missing_reported_after_cutoff():
    """周五 22:00 数据只到 11-05 → 报（当天缺当天报）。"""
    found = _run(dt.datetime(2026, 11, 6, 22, 0, tzinfo=CST),
                 {"daily_bar": D(2026, 11, 5)})
    assert _keys(found) == ["fresh_daily_bar"]


def test_before_cutoff_not_reported():
    """同一数据但只到 20:00（日更作业 18:40 才跑完，21:00 前不判）→ 不报。"""
    assert _keys(_run(dt.datetime(2026, 11, 6, 20, 0, tzinfo=CST),
                      {"daily_bar": D(2026, 11, 5)})) == []


def test_intraday_tables_checked_after_open():
    """交易日 10:00，7 张行情表都停在昨日 → 7 条（否则盘中停采无人知）。"""
    stale = {t: dt.datetime(2026, 11, 5, 23, 0, tzinfo=CST) for t in SESSION_TABLES}
    found = _run(dt.datetime(2026, 11, 6, 10, 0, tzinfo=CST), stale)
    assert _keys(found) == sorted("fresh_" + t for t in SESSION_TABLES)


# --------------------------------------------------------------------------
# 3. 夜盘跨零点 → 前一交易日
# --------------------------------------------------------------------------

def test_night_session_bar_belongs_to_previous_trade_day():
    """11-07 02:30 的桶属 11-06 交易日晚盘 → 不算滞后。"""
    assert _keys(_run(dt.datetime(2026, 11, 9, 0, 9, tzinfo=CST),
                      {"l2_adj.bar_60m_ba": dt.datetime(2026, 11, 7, 2, 30, tzinfo=CST)})) == []


def test_night_session_stall_reported():
    """夜盘停在 11-05 01:00（属 11-04）→ 缺 2 个交易日。"""
    found = _run(dt.datetime(2026, 11, 9, 10, 30, tzinfo=CST),
                 {"l2_adj.bar_60m_ba": dt.datetime(2026, 11, 5, 1, 0, tzinfo=CST)})
    assert _keys(found) == ["fresh_l2_adj.bar_60m_ba"]
    assert "缺 2 个交易日" in found[0]["msg"]


# --------------------------------------------------------------------------
# 4. 降级与边界
# --------------------------------------------------------------------------

def test_fallback_to_natural_days_when_calendar_unavailable():
    """futures_rule 不可用 → 退回自然日口径，宁可误报不可漏报。"""
    found = _run(dt.datetime(2026, 11, 9, 9, 0, tzinfo=CST),
                 {"daily_bar": D(2026, 11, 3)}, days=[])
    assert _keys(found) == ["fresh_daily_bar"]
    assert "数据滞后 6 天" in found[0]["msg"]


def test_stale_beyond_window_reported():
    """数据早于 120 交易日窗口也要报（不能被 _prev_trade_day 返回 None 静音）。"""
    found = _run(dt.datetime(2026, 11, 9, 9, 0, tzinfo=CST),
                 {"daily_bar": D(2019, 1, 2)})
    assert _keys(found) == ["fresh_daily_bar"]
    assert "疑似长期停更" in found[0]["msg"]


def test_every_table_has_explicit_cadence():
    """没有表被漏分类——漏了会静默落 natural，长假立刻误报。"""
    cfg = _cfg()
    missing = set(cfg.freshness_tables) - set(cfg.freshness_cadence)
    assert missing == set()
    assert set(cfg.freshness_cadence.values()) <= {"trade_day", "session", "natural"}
