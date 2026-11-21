# -*- coding: utf-8 -*-
"""CL-20261121-06 / 5M-P0C：池级单持仓闸单测（纯离线，不写 execution_position）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.routers import execution as exec_api
from app.api.routers.execution import SubmitOrderReq
from app.execution import persistence as exec_persistence
from app.strategies.five_min.pool_position import PoolPositionGuard

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 11, 21, 9, 0, tzinfo=TZ)


def _order(oid, action, symbol, status="FILLED", mins=0, pool="5m-core"):
    return {"id": oid, "pool_id": pool, "action": action, "real_symbol": symbol,
            "status": status, "signal_id": f"S{oid}", "created_at": T0 + timedelta(minutes=mins)}


def test_disabled_guard_is_noop():
    g = PoolPositionGuard(enabled=False)
    assert g.check_open("5m-core", "FG2701")[0] is True
    assert g.check_close("5m-core", "SR2701")[0] is True
    assert g.slot("5m-core") is None


def test_rebuild_from_order_ledger_not_position_table():
    g = PoolPositionGuard(enabled=True)
    rows = [_order(1, "OPEN", "FG2701", mins=0),
            _order(2, "CLOSE", "FG2701", mins=30),
            _order(3, "OPEN", "SR2701", mins=60)]
    slot = g.rebuild("5m-core", rows)
    assert slot is not None and slot.real_symbol == "SR2701" and slot.order_id == 3
    # 已平掉再开新仓后，旧合约不能再开、别的合约平仓要拒绝
    assert g.check_open("5m-core", "FG2701", sync=False)[0] is False
    assert g.check_close("5m-core", "FG2701", sync=False)[0] is False
    assert g.check_close("5m-core", "SR2701", sync=False)[0] is True


def test_dead_orders_do_not_create_position():
    g = PoolPositionGuard(enabled=True)
    rows = [_order(1, "OPEN", "FG2701", status="REJECTED"),
            _order(2, "OPEN", "SR2701", status="CANCELED"),
            _order(3, "OPEN", "MA2701", status="ERROR")]
    assert g.rebuild("5m-core", rows) is None
    assert g.check_open("5m-core", "FG2701", sync=False)[0] is True


def test_partial_and_new_open_orders_count_as_in_position():
    g = PoolPositionGuard(enabled=True)
    for st in ("NEW", "SENT", "PARTIAL"):
        rows = [_order(7, "OPEN", "EB2701", status=st)]
        assert g.rebuild("5m-core", rows).real_symbol == "EB2701"


def test_mark_open_close_without_db_sync():
    g = PoolPositionGuard(enabled=True)
    g.mark_open("5m-core", "FG2701", "S1", 11)
    assert g.check_open("5m-core", "MA2701", sync=False)[0] is False
    g.mark_close("5m-core", "MA2701")  # 错合约不清槽
    assert g.slot("5m-core").real_symbol == "FG2701"
    g.mark_close("5m-core", "FG2701")
    assert g.check_open("5m-core", "MA2701", sync=False)[0] is True


def test_api_wires_pool_gate_before_save_order(monkeypatch):
    """闸必须在 save_order 之前拒绝，绝不能先落单再拦截。"""
    saved: list[dict] = []
    monkeypatch.setattr(
        exec_api, "get_settings",
        lambda: SimpleNamespace(
            execution=SimpleNamespace(enabled=True, account="SIM", channel="sim")))
    monkeypatch.setattr(exec_persistence, "build_idempotency_key", lambda *a: "K")
    monkeypatch.setattr(exec_persistence, "find_order_by_signal",
                        lambda *a, **kw: None)
    monkeypatch.setattr(exec_persistence, "save_order",
                        lambda o: saved.append(o) or 99)

    class _Busy:
        def check_open(self, *a, **kw):
            return False, "池内已有在场单"

    monkeypatch.setattr("app.strategies.five_min.pool_position.PoolPositionGuard", _Busy)
    req = SubmitOrderReq(real_symbol="FG2701", exchange="CZCE", action="OPEN",
                         direction="BUY", price=1200.0, lots=2, pool_id="5m-core")
    res = exec_api.submit_order(req)
    assert res["state"] == "POOL_POSITION_BLOCKED" and not saved
