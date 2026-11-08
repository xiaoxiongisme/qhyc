"""融合策略微信推送盈亏金额契约（信号估算口径，非实际成交）。

CL-20261108-18：``68bfe82``（2026-11-06/07 同步 130 个生产 hotfix）曾把
「推送盈亏金额」整体下线，本文件一度成为僵尸测试（3 errors）。2026-11-08
用户拍板**恢复功能**，已按 ``68bfe82^`` 原契约原样复原：
  · ``_pnl_yuan`` = sign × (px − entry) × 乘数 × 手数，毛盈亏、未含费用；
  · 乘数唯一真源 ``dim_variety``（经 ``barstore.variety_spec``），
    取不到返回 None，**绝不按 1.0 估算**；
  · ``_fmt_money`` 输出 +/- 千分位整数元；
  · 推送正文含「估算浮盈合计」「平仓估算盈亏」与「非实际成交账户盈亏」免责声明。

⚠ 三个用例的断言自 dcb1d9b 起一字未改，恢复只动生产实现，不放宽契约。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.scheduler as sched

@pytest.fixture(autouse=True)
def _clear_pnl_caches():
    # 缓存是模块级 dict/set，用例之间必须清空，否则 monkeypatch 的 variety_spec
    # 会被上一个用例的缓存命中（假绿）。getattr 容错保留：符号若再次下线，
    # 这里不该比断言先炸。
    for name in ("_MULT_CACHE", "_DEC_CACHE", "_MULT_WARNED", "_DEC_WARNED"):
        obj = getattr(sched, name, None)
        if obj is not None and hasattr(obj, "clear"):
            obj.clear()
    yield


def _spec(_code, **_kwargs):
    return {"multiplier": 20.0, "tick_size": 1.0}


def test_pnl_yuan_long_short_and_lots(monkeypatch):
    import app.data.barstore as bs
    monkeypatch.setattr(bs, "variety_spec", _spec)
    assert sched._pnl_yuan("FG888", "LONG", 100, 110, 2) == 400.0
    assert sched._pnl_yuan("FG888", "SHORT", 110, 100, 2) == 400.0
    assert sched._pnl_yuan("FG888", "LONG", 110, 100, 1) == -200.0
    assert sched._pnl_yuan("FG888", "FLAT", 100, 110, 1) is None


def test_pnl_yuan_missing_multiplier_never_defaults_to_one(monkeypatch):
    import app.data.barstore as bs
    from app.data.barstore import VarietySpecNotFoundError

    def _raise(_code, **_kwargs):
        raise VarietySpecNotFoundError("missing")

    monkeypatch.setattr(bs, "variety_spec", _raise)
    assert sched._pnl_yuan("ZZZ888", "LONG", 100, 110, 1) is None
    assert sched._fmt_money(None) == "-"


def test_format_push_contains_position_and_close_amounts(monkeypatch):
    import app.data.barstore as bs
    monkeypatch.setattr(bs, "variety_spec", _spec)
    now = datetime(2026, 11, 2, 10, 5, tzinfo=timezone(timedelta(hours=8)))
    rows = [{
        "symbol": "FG888", "label": "FG 玻璃", "icon": "🔴", "side": "LONG",
        "entry": 100.0, "px": 110.0, "pnl": 0.10, "pnl_yuan": 200.0,
        "lots": 1, "stop": 95.0, "be_trigger": 105.0, "be_done": False,
        "age_min": 5.0, "latest_dt": now,
    }]
    signals = [{
        "symbol": "FG888", "label": "FG 玻璃", "action": "平（平多）",
        "to": "FLAT", "close": 110.0, "entry": 100.0, "new_entry": None,
        "pnl": 0.10, "pnl_yuan": 200.0, "lots": 1,
    }]
    _title, content = sched._format_push(
        rows, signals, [], [], now, kind="signal", max_rows=30, show_levels=True)
    assert "估算浮盈合计 **+200元**" in content
    assert "平仓估算盈亏 **+200元**（+10.0%，未含费用）" in content
    assert "非实际成交账户盈亏" in content
