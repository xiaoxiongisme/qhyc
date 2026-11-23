# -*- coding: utf-8 -*-
"""CL-20261123-03：成交回报写库链路验收（sim 零真实资金）。

跑法（容器内）：
    docker exec -w /app -e PYTHONPATH=/app -e EXECUTION_BROKER=sim qhyc-api \
        python -m pytest tests/test_execution_fill_writeback.py -v
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.execution import execution_runtime, persistence, sim_broker

pytestmark = pytest.mark.requires_db

_REAL = "RB2701"


def _mk(sig: str, *, lots: int = 2, price: float = 3112.0, **kw):
    base = dict(
        signal_id=sig, symbol_continuous="RB888", real_symbol=_REAL, exchange="SHFE",
        action="OPEN", direction="BUY", price=price, price_space="raw", lots=lots,
        multiplier=10, notional=price * lots * 10, channel="SIM", account=None,
    )
    base.update(kw)
    base["idempotency_key"] = persistence.build_idempotency_key(
        sig, _REAL, "OPEN", date.today())
    return base


@pytest.fixture(autouse=True)
def _clean():
    sim_broker.reset()
    yield
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        s.execute(text("DELETE FROM execution_order WHERE signal_id LIKE 'TEST-FILL-%'"))


def _fills(oid: int):
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        return [dict(r) for r in s.execute(
            text("SELECT * FROM execution_fill WHERE order_id=:id ORDER BY id"),
            {"id": oid},
        ).mappings().all()]


# ------------------------------- DB -------------------------------
def test_ack_with_fill_writes_execution_fill():
    """ACK 自带成交明细 → execution_fill 落库且状态直接 FILLED。"""
    sig = f"TEST-FILL-ACK-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig, lots=2))
    res = execution_runtime.submit_order(oid)

    assert res["state"] == "ACK" and res["fill_lots_saved"] == 2
    assert persistence.get_order(oid)["status"] == "FILLED"
    rows = _fills(oid)
    assert len(rows) == 1
    assert int(rows[0]["fill_lots"]) == 2
    assert Decimal(str(rows[0]["fill_price"])) == Decimal("3112.0")
    assert rows[0]["broker_fill_id"] == f"{res['broker_order_id']}#2@3112.0"


def test_partial_ack_and_reconcile_are_idempotent():
    """部分成交 ACK → PARTIAL；重复查单不重复记账（UNIQUE 兜底）。"""
    sig = f"TEST-FILL-PART-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig, lots=2))
    payload = dict(real_symbol=_REAL, direction="BUY", action="OPEN", price=3112.0,
                   lots=2, channel="SIM", account=None, _simulate="partial")
    res = execution_runtime.submit_order(oid, payload=payload)
    assert res["fill_lots_saved"] == 1
    assert persistence.get_order(oid)["status"] == "PARTIAL"
    assert len(_fills(oid)) == 1

    for _ in range(2):
        st = execution_runtime.reconcile_order(oid)
        assert st["mapped"] == "PARTIAL"
        assert len(_fills(oid)) == 1


def test_reconcile_backfills_fill_for_queued_order():
    """限价挂单 submit 无成交；on_tick 成交后查单必须补写 execution_fill。"""
    sig = f"TEST-FILL-QUE-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig, lots=1))
    payload = dict(real_symbol=_REAL, direction="BUY", action="OPEN", price=1200.0,
                   lots=1, channel="SIM", account=None, price_type="LIMIT",
                   queue_limit=True)
    res = execution_runtime.submit_order(oid, payload=payload)
    assert res["fill_lots_saved"] == 0 and not _fills(oid)
    assert persistence.get_order(oid)["status"] == "SENT"

    bid = res["broker_order_id"]
    tick = sim_broker.on_tick(bid, 1198.0)
    assert tick["status"] == "FILLED"

    st = execution_runtime.reconcile_order(oid)
    assert st["mapped"] == "FILLED" and st["fill_lots_saved"] == 1
    assert persistence.get_order(oid)["status"] == "FILLED"
    rows = _fills(oid)
    assert len(rows) == 1 and Decimal(str(rows[0]["fill_price"])) == Decimal("1198.0")


# ------------------------------- 离线 fail-loud -------------------------------
def test_incomplete_fill_payload_fails_loud(monkeypatch):
    """只有手数没有均价 → ValueError，禁止按 0 兜底写脏成交。"""
    calls: list[str] = []
    monkeypatch.setattr(
        execution_runtime.persistence, "get_order",
        lambda _id: {"id": 1, "status": "NEW", "retry_count": 0,
                     "real_symbol": _REAL, "direction": "BUY", "action": "OPEN",
                     "price": 3112.0, "lots": 2, "channel": "SIM", "account": None})
    monkeypatch.setattr(
        execution_runtime.persistence, "update_status",
        lambda *a, **kw: calls.append("status"))
    monkeypatch.setattr(
        execution_runtime.persistence, "save_fill",
        lambda *a, **kw: calls.append("fill"))

    class _Broker:
        def submit(self, body):
            return {"state": "ACK", "broker_order_id": "SIMX", "filled_lots": 2}

    monkeypatch.setattr(execution_runtime, "get_broker", lambda: _Broker())
    with pytest.raises(ValueError, match="fail-loud"):
        execution_runtime.submit_order(1)
    assert "fill" not in calls


def test_plain_ack_does_not_invent_fill(monkeypatch):
    """纯 ACK（真实网关常见）不带成交字段 → 不写 execution_fill。"""
    calls: list[str] = []
    monkeypatch.setattr(
        execution_runtime.persistence, "get_order",
        lambda _id: {"id": 2, "status": "NEW", "retry_count": 0,
                     "real_symbol": _REAL, "direction": "BUY", "action": "OPEN",
                     "price": 3112.0, "lots": 2, "channel": "SIM", "account": None})
    monkeypatch.setattr(execution_runtime.persistence, "update_status",
                        lambda *a, **kw: None)
    monkeypatch.setattr(execution_runtime.persistence, "save_fill",
                        lambda *a, **kw: calls.append("fill"))

    class _Broker:
        def submit(self, body):
            return {"state": "ACK", "broker_order_id": "SIMX"}

    monkeypatch.setattr(execution_runtime, "get_broker", lambda: _Broker())
    res = execution_runtime.submit_order(2)
    assert res["fill_lots_saved"] == 0 and calls == []
