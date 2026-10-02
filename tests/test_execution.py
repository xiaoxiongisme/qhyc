# -*- coding: utf-8 -*-
"""P0-1 执行反解层单元测试（离线，monkeypatch 掉 DB 依赖）。

覆盖：手数计算、价空间反解、交割月护栏、tick 对齐、元数据回退、service 编排。
运行：PYTHONPATH=. python -m pytest tests/test_execution.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import app.execution.lot_sizing as ls
import app.execution.reverse_price as rp
import app.execution.roll_policy as rl
import app.execution.service as svc
from app.execution.schemas import ReverseRequest


# ---------------- 手数计算 ----------------
def test_validate_given_lots():
    assert ls.validate_given_lots(5) == (5, [])
    assert ls.validate_given_lots(0) == (0, ["lots=0 < 1，非法"])
    lots, warns = ls.validate_given_lots(11)
    assert lots == 11 and warns  # 超上限告警但仍返回


def test_from_target_risk(monkeypatch):
    monkeypatch.setattr(ls, "MAX_LOTS", 30)  # 隔离容器/本地 env 差异
    # per_lot_risk = atr(50) * multiplier(10) = 500；target 10000 → 20 手
    lots, warns = ls.from_target_risk(10000, 50, 10, 3112.0)
    assert lots == 20 and not warns
    # 目标过小 → 至少 1 手
    lots, warns = ls.from_target_risk(100, 50, 10, 3112.0)
    assert lots == 1 and warns
    # 超上限 → 截断到 MAX_LOTS 并告警
    monkeypatch.setattr(ls, "MAX_LOTS", 5)
    lots, warns = ls.from_target_risk(10000, 50, 10, 3112.0)
    assert lots == 5 and warns
    # atr 缺失 → 0 手 + 告警
    lots, warns = ls.from_target_risk(10000, None, 10, 3112.0)
    assert lots == 0 and warns


# ---------------- 价空间反解 ----------------
def test_reverse_price_raw(monkeypatch):
    monkeypatch.setattr(rp, "offset_at", lambda *a, **k: 2400.0)
    # raw：偏移置 0
    real, cum = rp.reverse_price(None, "RB888", 3112.0, datetime(2026, 9, 30, 15), "raw")
    assert real == 3112.0 and cum == 0.0
    # adj：减偏移
    real, cum = rp.reverse_price(None, "RB888", 3112.0, datetime(2026, 9, 30, 15), "adj")
    assert real == 712.0 and cum == 2400.0


# ---------------- 交割月护栏 ----------------
def test_delivery_guard():
    # 2027-01 合约，最后交易日约 2027-01-15；信号日 2027-01-12 在缓冲内 → 阻断
    assert rl.delivery_guard("FG2701", date(2027, 1, 12))
    # 远月 → 通过
    assert rl.delivery_guard("FG2701", date(2026, 10, 1)) == []


# ---------------- tick 对齐 ----------------
def test_check_tick():
    assert rp is not None
    from app.execution.validator import check_tick
    assert check_tick(3112.0, 1.0) == []           # 对齐
    assert check_tick(3112.5, 1.0)                 # 非对齐 → 告警
    assert check_tick(3112.0, None)                 # tick 未知 → 跳过告警


# ---------------- 元数据回退 ----------------
class _FakeSym:
    def __init__(self, multiplier=None, price_tick=None, active=True, exchange="CZCE"):
        self.multiplier = multiplier
        self.price_tick = price_tick
        self.active = active
        self.exchange = exchange


class _FakeRepo:
    def __init__(self, table):
        self._t = table

    def get(self, symbol):
        return self._t.get(symbol)


def test_real_contract_metadata_fallback(monkeypatch):
    # futures_symbol 不含真实合约 FG2701，但含连续 FG888（multiplier=20）
    repo = _FakeRepo({"FG888": _FakeSym(multiplier=20, price_tick=1.0)})
    monkeypatch.setattr(rl, "SymbolRepository", lambda s: repo)
    meta = rl.real_contract_metadata(None, "FG2701")
    assert meta["source"] == "continuous_fallback"
    assert meta["multiplier"] == 20 and meta["price_tick"] == 1.0
    # futures_symbol 直接命中
    repo2 = _FakeRepo({"FG2701": _FakeSym(multiplier=20, price_tick=1.0, active=False)})
    monkeypatch.setattr(rl, "SymbolRepository", lambda s: repo2)
    meta2 = rl.real_contract_metadata(None, "FG2701")
    assert meta2["source"] == "futures_symbol" and meta2["active"] is False


# ---------------- service 编排 ----------------
def test_resolve_signal_to_order(monkeypatch):
    monkeypatch.setattr(svc, "select_real_contract",
                        lambda s, sym, td: ("FG2701", "CZCE", False, "FG888"))
    monkeypatch.setattr(svc, "delivery_guard", lambda *a, **k: [])
    monkeypatch.setattr(svc, "active_guard", lambda *a, **k: [])
    monkeypatch.setattr(svc, "real_contract_metadata",
                        lambda s, r: {"exchange": "CZCE", "multiplier": 20.0,
                                      "price_tick": 1.0, "active": True,
                                      "source": "continuous_fallback"})
    monkeypatch.setattr(svc, "reverse_price",
                        lambda s, sym, px, when, space=None: (px, 0.0))
    monkeypatch.setattr(svc, "validate", lambda *a, **k: ([], []))

    req = ReverseRequest(symbol="FG888", trade_date=date(2026, 9, 30),
                         direction="LONG", entry_px=969.0, lots=3)
    order = svc.resolve_signal_to_order(object(), req)
    assert order.real_symbol == "FG2701"
    assert order.price == 969.0
    assert order.lots == 3
    assert order.notional == 3 * 20 * 969.0
    assert order.ok

    # FLAT 直接返回空单
    req_f = ReverseRequest(symbol="FG888", trade_date=date(2026, 9, 30), direction="FLAT")
    o_f = svc.resolve_signal_to_order(object(), req_f)
    assert o_f.lots == 0 and "FLAT" in o_f.warnings[0]

    # 非连续码 → 阻断
    req_bad = ReverseRequest(symbol="FG2701", trade_date=date(2026, 9, 30),
                             direction="LONG", entry_px=969.0, lots=1)
    o_bad = svc.resolve_signal_to_order(object(), req_bad)
    assert o_bad.blocking_reasons and o_bad.lots == 0
