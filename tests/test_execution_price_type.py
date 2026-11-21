# -*- coding: utf-8 -*-
"""CL-20261121-05 / 5M-P0B：price_type 全链路 + sim 挂单生命周期单测（离线）。"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.routers.execution import SubmitOrderReq, build_order_dict
from app.execution import execution_runtime, sim_broker


def _req(**kw):
    base = dict(real_symbol="FG2701", exchange="CZCE", action="OPEN",
                direction="BUY", price=1200.0, lots=2)
    base.update(kw)
    return SubmitOrderReq(**base)


def test_request_default_limit_and_validation():
    assert _req().price_type == "LIMIT"
    assert _req(price_type="MARKET").price_type == "MARKET"
    with pytest.raises(ValidationError):
        _req(price_type="STOP")


def test_build_order_dict_carries_price_type_and_pool():
    d = build_order_dict(_req(price_type="MARKET", pool_id="5m-core"),
                         "SIM", "sim", "K1")
    assert d["price_type"] == "MARKET" and d["pool_id"] == "5m-core"
    d2 = build_order_dict(_req(), None, None, "K2")
    assert d2["price_type"] == "LIMIT" and d2["pool_id"] is None


def test_runtime_payload_forwards_price_type(monkeypatch):
    sent: dict = {}

    class _Broker:
        def submit(self, body):
            sent.update(body)
            return {"state": "ACK", "broker_order_id": "SIMX"}

    monkeypatch.setattr(
        execution_runtime.persistence, "get_order",
        lambda _id: {"status": "NEW", "retry_count": 0, "real_symbol": "FG2701",
                     "direction": "BUY", "action": "OPEN", "price": 1200,
                     "price_type": "LIMIT", "pool_id": "5m-core",
                     "lots": 2, "channel": "sim", "account": "SIM"})
    monkeypatch.setattr(execution_runtime.persistence, "update_status",
                        lambda *a, **kw: None)
    monkeypatch.setattr(execution_runtime, "get_broker", lambda: _Broker())
    execution_runtime.submit_order(1)
    assert sent["price_type"] == "LIMIT" and sent["pool_id"] == "5m-core"


def test_sim_default_still_fills_immediately():
    sim_broker.reset()
    res = sim_broker.submit({"price": 1200.0, "lots": 2, "direction": "BUY",
                             "action": "OPEN", "price_type": "LIMIT"})
    assert res["state"] == "ACK" and res["filled_lots"] == 2
    assert sim_broker.query_order(res["broker_order_id"])["status"] == "FILLED"


def test_sim_limit_queue_lifecycle():
    """挂单模式必须能测出「不成交 / 触及成交 / 撤单」三种真实形态。"""
    sim_broker.reset()
    bid = sim_broker.submit({"price": 1200.0, "lots": 2, "direction": "BUY",
                             "action": "OPEN", "price_type": "LIMIT",
                             "queue_limit": True})["broker_order_id"]
    assert sim_broker.query_order(bid)["status"] == "NEW"
    assert sim_broker.on_tick(bid, 1205.0)["status"] == "NEW"  # 未触及
    filled = sim_broker.on_tick(bid, 1198.0)
    assert filled["status"] == "FILLED" and filled["avg_fill_price"] == 1198.0
    assert sim_broker.cancel(bid)["state"] == "REJECT"  # 已成交不可撤

    bid2 = sim_broker.submit({"price": 1200.0, "lots": 1, "direction": "SELL",
                              "action": "OPEN", "price_type": "LIMIT",
                              "queue_limit": True})["broker_order_id"]
    assert sim_broker.on_tick(bid2, 1190.0)["status"] == "NEW"
    assert sim_broker.on_tick(bid2, 1202.0)["avg_fill_price"] == 1202.0
    bid3 = sim_broker.submit({"price": 1200.0, "lots": 1, "direction": "BUY",
                              "action": "OPEN", "price_type": "LIMIT",
                              "queue_limit": True})["broker_order_id"]
    assert sim_broker.cancel(bid3)["state"] == "OK"


def test_migration_declares_columns_and_switch():
    text = (Path(__file__).resolve().parents[1] / "migrations"
            / "083_execution_price_type_and_pool.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS price_type" in text
    assert "ADD COLUMN IF NOT EXISTS pool_id" in text
    assert "five_min_pool_position_enabled" in text
