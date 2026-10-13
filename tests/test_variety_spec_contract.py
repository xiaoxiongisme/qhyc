"""``barstore.variety_spec`` fail-loud 契约测试（全平台取数闸门基石）。

variety_spec 是「品种规格」唯一真源，**刻意不提供默认值**：品种缺失 / 已下线
(is_active=false) / multiplier 为空都必须抛 :class:`VarietySpecNotFoundError`
—— 乘数错一次 = P&L 错一整轮回测（螺纹 10→1、沪金 1000→1 静默兜底事故）。

用 fake engine/connection 注入受控行，无需真实 DB。
"""
from __future__ import annotations

import pytest

import app.data.barstore as bs
from app.data.barstore import VarietySpecNotFoundError

# 与 variety_spec 的 SELECT 列序一致：
# (variety_code, variety_name, exchange, multiplier, tick_size, quote_unit, main_symbol, is_active)
_GOOD = ("RB", "螺纹钢", "SHFE", 10.0, 1.0, "元/吨", "RB888", True)


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Conn:
    def __init__(self, main_row, tick_row=None):
        self._main = main_row
        self._tick = tick_row

    def execute(self, stmt, params=None):
        return _Result(self._tick if "tick_history" in str(stmt) else self._main)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Engine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _patch(monkeypatch, main_row, tick_row=None):
    monkeypatch.setattr(bs, "variety_of", lambda s, **k: "RB")
    monkeypatch.setattr(bs, "_engine", lambda: _Engine(_Conn(main_row, tick_row)))


def test_returns_fields(monkeypatch):
    _patch(monkeypatch, _GOOD)
    spec = bs.variety_spec("RB888", use_cache=False)
    assert spec["variety_code"] == "RB"
    assert spec["exchange"] == "SHFE"
    assert spec["multiplier"] == 10.0
    assert spec["tick_size"] == 1.0
    assert spec["quote_unit"] == "元/吨"


def test_tick_history_takes_priority(monkeypatch):
    # tick 时序化：有历史行则用历史 tick，覆盖 dim_variety.tick_size
    _patch(monkeypatch, _GOOD, tick_row=(2.0,))
    spec = bs.variety_spec("RB888", use_cache=False)
    assert spec["tick_size"] == 2.0


def test_missing_row_raises(monkeypatch):
    _patch(monkeypatch, None)
    with pytest.raises(VarietySpecNotFoundError):
        bs.variety_spec("ZZZ", use_cache=False)


def test_inactive_variety_raises(monkeypatch):
    _patch(monkeypatch, ("RB", "螺纹钢", "SHFE", 10.0, 1.0, "元/吨", "RB888", False))
    with pytest.raises(VarietySpecNotFoundError):
        bs.variety_spec("RB", use_cache=False)


def test_null_multiplier_raises_never_defaults_to_one(monkeypatch):
    # 核心契约：multiplier 为空必须抛，绝不静默按 1.0 兜底
    _patch(monkeypatch, ("RB", "螺纹钢", "SHFE", None, 1.0, "元/吨", "RB888", True))
    with pytest.raises(VarietySpecNotFoundError):
        bs.variety_spec("RB", use_cache=False)
