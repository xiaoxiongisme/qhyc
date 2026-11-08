# -*- coding: utf-8 -*-
"""持仓同步收口（CL-20261108-26）。

背景：2026-11-08 核查发现「持仓同步」在代码里不存在——`execution_position`
只能由 `persistence.upsert_position` 写，唯一业务调用方 `position_manager.apply_fill`
**零调用方**；而 `bridge_report` 的注释却声称「持仓由 upsert_position 维护」。
本次把它收口成 `app/execution/position_sync.py` 一个入口，并用开关默认关闭。

这组测试钉四件事：
  1. 开关关闭时，**任何路径都不碰持仓表**（连 apply_fill 都不进去）；
  2. 开关关闭是「可观测的未同步」，返回值带原因，不是假装成功；
  3. 开关开启时，成交回报按增量滚动、手工快照按绝对值覆盖；
  4. 自相矛盾的快照被拒绝，不做「尽力修正」。
"""
from __future__ import annotations

import pytest

from app.execution import position_sync


class _FakePos:
    net_lots = 2
    long_lots = 2
    short_lots = 0
    today_lots = 1


def test_disabled_blocks_fill_sync(monkeypatch):
    """开关关闭 → 不进入 position_manager（哪怕成交回报完全合法）。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: False)
    called: list[tuple] = []

    from app.execution import position_manager
    monkeypatch.setattr(position_manager, "apply_fill",
                        lambda **kw: called.append(kw) or _FakePos())

    r = position_sync.sync_from_fill(
        real_symbol="RB888", action="OPEN", direction="BUY", fill_lots=2, fill_price=3500.0)

    assert r.applied is False
    assert "position_sync_disabled" in r.reason
    assert called == [], "开关关闭时绝不应调用 apply_fill"


def test_disabled_blocks_snapshot_sync(monkeypatch):
    """开关关闭 → 手工快照同样不写库。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: False)

    from app.execution import persistence
    called: list[dict] = []
    monkeypatch.setattr(persistence, "upsert_position",
                        lambda **kw: called.append(kw))

    r = position_sync.sync_from_snapshot(real_symbol="RB888", net_lots=3)

    assert r.applied is False
    assert called == [], "开关关闭时绝不应写 execution_position"


def test_enabled_fill_rolls_position(monkeypatch):
    """开关开启 → 成交回报进入 apply_fill 并返回新快照。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: True)
    seen: list[dict] = []

    from app.execution import position_manager
    monkeypatch.setattr(position_manager, "apply_fill",
                        lambda **kw: seen.append(kw) or _FakePos())

    r = position_sync.sync_from_fill(
        real_symbol="RB888", action="OPEN", direction="BUY",
        fill_lots=2, fill_price=3500.0, channel="INFINITRADER", account="A1")

    assert r.applied is True
    assert r.position == {"net_lots": 2, "long_lots": 2,
                          "short_lots": 0, "today_lots": 1}
    assert seen and seen[0]["action"] == "OPEN" and seen[0]["direction"] == "BUY"
    assert seen[0]["channel"] == "INFINITRADER"   # 口径要透传，不能悄悄退回 SIM


def test_enabled_snapshot_overwrites(monkeypatch):
    """开关开启 → 手工快照按绝对值覆盖，且 long/short 可由 net 推出。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: True)
    seen: list[dict] = []

    from app.execution import persistence
    monkeypatch.setattr(persistence, "upsert_position",
                        lambda **kw: seen.append(kw))

    r = position_sync.sync_from_snapshot(real_symbol="JD888", net_lots=-3, today_lots=1)

    assert r.applied is True
    assert seen[0]["net_lots"] == -3
    assert seen[0]["long_lots"] == 0 and seen[0]["short_lots"] == 3


def test_snapshot_rejects_inconsistent(monkeypatch):
    """net 与 long-short 矛盾 → 拒绝写入（不猜哪个数对）。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: True)

    from app.execution import persistence
    called: list[dict] = []
    monkeypatch.setattr(persistence, "upsert_position", lambda **kw: called.append(kw))

    r = position_sync.sync_from_snapshot(
        real_symbol="RB888", net_lots=2, long_lots=0, short_lots=0)

    assert r.applied is False
    assert "inconsistent_snapshot" in r.reason
    assert called == []


def test_fill_rejects_zero_lots(monkeypatch):
    """成交手数 <=0 → 不回写（宁可台账不动，也不按 0 手记一笔成交）。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: True)

    from app.execution import position_manager
    called: list[dict] = []
    monkeypatch.setattr(position_manager, "apply_fill", lambda **kw: called.append(kw))

    r = position_sync.sync_from_fill(
        real_symbol="RB888", action="OPEN", direction="BUY", fill_lots=0)

    assert r.applied is False
    assert "invalid_fill_lots" in r.reason
    assert called == []


def test_switch_key_matches_migration():
    """开关键必须与 migrations/060 一字不差，否则读不到 → 永远走默认关闭。"""
    assert position_sync.SWITCH_KEY == "position_sync_enabled"


def test_status_reports_reason(monkeypatch):
    """关闭状态要能说清「为什么没同步」，而不是只回一个 false。"""
    monkeypatch.setattr(position_sync, "is_sync_enabled", lambda: False)
    st = position_sync.status()
    assert st["enabled"] is False
    assert "关闭" in st["note"]
    assert st["switch_key"] == "position_sync_enabled"
