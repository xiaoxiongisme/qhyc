"""融合策略微信推送盈亏金额契约（信号估算口径，非实际成交）。

⚠ 2026-11-08 / CL-20261108-18：本文件是**僵尸测试**，整组 skip，不得删除。
``68bfe82``（2026-11-06/07 同步 130 个生产 hotfix）已把「推送盈亏金额」整体下线：
``app/scheduler.py`` 中 ``_MULT_CACHE`` / ``_MULT_WARNED`` / ``_pnl_yuan`` 均已不存在，
``pnl_yuan`` 在 app/ 与 scripts/ 全仓 0 命中。此前 CI 在这 3 个用例的 autouse
fixture 阶段就 AttributeError（3 errors），是 CI 常年判红的存量欠债。
不得以「让测试变绿」为由删文件或放宽断言；功能若要恢复或测试若要重写，
须先关闭 CL-20261108-18 并重新定义契约。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.scheduler as sched

pytestmark = pytest.mark.skip(
    reason="CL-20261108-18: 推送盈亏金额功能已于 68bfe82 下线，"
           "本文件测的 _pnl_yuan/_MULT_CACHE 已不存在，待重新定义契约")


@pytest.fixture(autouse=True)
def _clear_pnl_caches():
    # 符号可能已随功能下线而消失；用 getattr 容错，避免 skip 前 fixture 先炸。
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
