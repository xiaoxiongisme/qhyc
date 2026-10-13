"""G1 开平标志推导（资金事故面）纯逻辑契约测试。

``position_manager.derive_action`` 由「目标方向 + 当前持仓」推导开平标志，
**禁止用 direction 单独推断** —— 开错方向会直接变成反向开仓（真实资金事故，
见模块 docstring 与 DDL 030）。该函数纯逻辑、无 I/O，却历史零覆盖。

本测试锁定其全部规则分支：空仓/同向加仓=OPEN；反向平仓优先平今、无今仓平昨；
CZCE 无平今指令一律 CLOSE；非法入参 fail-loud。
"""
from __future__ import annotations

import pytest

from app.execution.position_manager import Position, derive_action


def _pos(net: int = 0, today: int = 0) -> Position:
    return Position(net_lots=net, long_lots=max(net, 0),
                    short_lots=max(-net, 0), today_lots=today)


# ---------- 开仓 ----------
def test_flat_opens():
    assert derive_action(direction="BUY", pos=_pos(0), exchange="SHFE") == "OPEN"
    assert derive_action(direction="SELL", pos=_pos(0), exchange="DCE") == "OPEN"


def test_same_direction_adds_as_open():
    # 已持多再 BUY = 同向加仓 → OPEN（绝不是平仓）
    assert derive_action(direction="BUY", pos=_pos(net=3, today=1), exchange="SHFE") == "OPEN"
    # 已持空再 SELL = 同向加仓 → OPEN
    assert derive_action(direction="SELL", pos=_pos(net=-3, today=1), exchange="DCE") == "OPEN"


# ---------- 反向平仓：平今优先，无今仓平昨 ----------
def test_reverse_close_prefers_today():
    assert derive_action(direction="BUY", pos=_pos(net=-3, today=2), exchange="SHFE") == "CLOSE_TODAY"
    assert derive_action(direction="SELL", pos=_pos(net=3, today=2), exchange="DCE") == "CLOSE_TODAY"


def test_reverse_close_yest_when_no_today():
    assert derive_action(direction="BUY", pos=_pos(net=-3, today=0), exchange="SHFE") == "CLOSE_YEST"
    assert derive_action(direction="SELL", pos=_pos(net=3, today=0), exchange="DCE") == "CLOSE_YEST"


# ---------- 交易所差异：CZCE 无平今指令 ----------
def test_czce_close_has_no_today_variant():
    assert derive_action(direction="BUY", pos=_pos(net=-3, today=2), exchange="CZCE") == "CLOSE"
    assert derive_action(direction="SELL", pos=_pos(net=3, today=0), exchange="CZCE") == "CLOSE"


# ---------- fail-loud ----------
def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        derive_action(direction="LONG", pos=_pos(0), exchange="SHFE")


def test_invalid_exchange_raises():
    # 未知交易所不默认按 SHFE 处理，直接抛
    with pytest.raises(ValueError):
        derive_action(direction="BUY", pos=_pos(0), exchange="NASDAQ")
