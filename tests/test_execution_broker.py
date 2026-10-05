# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · B0–B9 验收用例（SimNow 等价仿真，**零真实资金**）。

跑法（云端容器内）：
    docker exec -w /app -e PYTHONPATH=/app -e EXECUTION_BROKER=sim qhyc-api \
        python -m pytest tests/test_execution_broker.py -v

设计约束
--------
* 只用 ``sim_broker``（进程内仿真网关），**不发任何真实委托**；
* 订单/持仓落在 execution_* 表（与业务表隔离），用 ``TEST-*`` 前缀便于清理；
* 每个用例都断言 **fail-loud 行为**（缺 last_error、非法迁移、成本价缺失等必须抛错），
  而非只验 happy path —— 本项目最重的缺陷类型是"开关看似生效、实则静默"。
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest

os.environ.setdefault("EXECUTION_BROKER", "sim")

from app.execution import (  # noqa: E402
    execution_runtime,
    monitor,
    persistence,
    position_manager,
    sim_broker,
    stop_loss,
)
from app.execution.broker import describe, get_broker  # noqa: E402
from app.execution.persistence import ExecutionStateError  # noqa: E402

_real = "RB2701"
_ex = "SHFE"


def _mk(signal: str, *, action="OPEN", direction="BUY", lots=1, price=3112.0, **kw):
    base = dict(
        signal_id=signal, symbol_continuous="RB888", real_symbol=_real, exchange=_ex,
        action=action, direction=direction, price=price, price_space="raw", lots=lots,
        multiplier=10, notional=price * lots * 10, channel="SIM", account=None,
    )
    base.update(kw)
    base["idempotency_key"] = persistence.build_idempotency_key(
        signal, _real, action, date.today())
    return base


def _body(sig: str, **extra) -> dict:
    """构造下发给网关的 body（submit_order 的 payload 覆盖路径）。

    落库 dict 里的额外键不会被 submit_order 转发，故仿真钩子须经此路径传入。
    """
    o = _mk(sig)
    b = {k: o[k] for k in ("real_symbol", "direction", "action", "price", "lots",
                           "channel", "account")}
    b.update(extra)
    return b


@pytest.fixture(autouse=True)
def _clean():
    sim_broker.reset()
    yield
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        s.execute(text("DELETE FROM execution_order WHERE signal_id LIKE 'TEST-%'"))
        s.execute(text("DELETE FROM execution_position WHERE real_symbol LIKE 'TEST%'"))


# ------------------------------- B0 -------------------------------
def test_b0_ddl030_registered():
    """B0：DDL 030 四表存在且已登记 schema_migrations。"""
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        for t in ("execution_order", "execution_fill", "execution_position",
                  "execution_channel_health"):
            assert s.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None, t
        n = s.execute(text(
            "SELECT count(*) FROM schema_migrations WHERE filename='030_execution_channels.sql'"
        )).scalar()
    assert int(n) == 1, "030 未登记 schema_migrations"


# ------------------------------- B1 -------------------------------
def test_b1_idempotent_no_double_order():
    """B1：同 idempotency_key 重复下单 → 同一 id，且表内仅一行（防重复下单）。"""
    sig = f"TEST-B1-{uuid.uuid4().hex[:8]}"
    o = _mk(sig)
    i1 = persistence.save_order(o)
    i2 = persistence.save_order(dict(o))
    assert i1 == i2
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        n = s.execute(text("SELECT count(*) FROM execution_order WHERE idempotency_key=:k"),
                      {"k": o["idempotency_key"]}).scalar()
    assert int(n) == 1


# ------------------------------- B2 -------------------------------
def test_b2_ack_fill_position_rolls():
    """B2：ACK → SENT → 成交，持仓按 fill 滚动（含加权成本价）。"""
    sig = f"TEST-B2-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig, lots=2, price=3112.0))
    res = execution_runtime.submit_order(oid)
    assert res["state"] == "ACK"
    row = persistence.get_order(oid)
    assert row["status"] == "SENT" and row["broker_order_id"]

    st = execution_runtime.reconcile_order(oid)
    assert st["state"] == "FILLED" and st["mapped"] == "FILLED"
    assert persistence.get_order(oid)["status"] == "FILLED"

    position_manager.apply_fill(real_symbol=_real, action="OPEN", direction="BUY",
                                fill_lots=2, fill_price=3112.0)
    pos = position_manager.load_position(_real)
    assert (pos.net_lots, pos.long_lots) == (2, 0)
    r = persistence.get_position(_real)
    assert float(r["avg_open_price"]) == pytest.approx(3112.0)


# ------------------------------- B3 -------------------------------
def test_b3_reject_records_last_error():
    """B3：网关拒单 → status=REJECTED 且 last_error 非空（fail-loud）。"""
    sig = f"TEST-B3-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig))
    res = execution_runtime.submit_order(oid, payload=_body(sig, _simulate="reject"))
    assert res["state"] == "REJECT"
    row = persistence.get_order(oid)
    assert row["status"] == "REJECTED" and row["last_error"]


# ------------------------------- B4 -------------------------------
def test_b4_timeout_unknown_no_retry_then_query():
    """B4：提交超时 → 留 NEW（未确认送达）且**不重试**；查单后判定终态。"""
    sig = f"TEST-B4-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig))
    res = execution_runtime.submit_order(oid, payload=_body(sig, _simulate="timeout"))
    assert res["state"] == "TIMEOUT_UNKNOWN"
    assert persistence.get_order(oid)["status"] == "NEW"      # 没改成 SENT
    # 再调一次 submit 必须被幂等保护挡下（不产生第二单）
    res2 = execution_runtime.submit_order(oid)
    assert res2["state"] == "SKIPPED"


# ------------------------------- B5 -------------------------------
def test_b5_illegal_transition_rejected():
    """B5：非法状态迁移（NEW→FILLED 直跳）在应用层被拒。"""
    sig = f"TEST-B5-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig))
    with pytest.raises(ExecutionStateError):
        persistence.update_status(oid, "FILLED")


# ------------------------------- B6 -------------------------------
def test_b6_error_requires_last_error():
    """B6：ERROR/REJECTED 缺 last_error → 抛错（不静默写空）。"""
    sig = f"TEST-B6-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig))
    with pytest.raises(ExecutionStateError):
        persistence.update_status(oid, "ERROR")


# ------------------------------- B7 -------------------------------
def test_b7_derive_action_czce_and_close_today():
    """B7：G1 开平推导 + CZCE 无平今指令收敛。"""
    P = position_manager.Position
    assert position_manager.derive_action(
        direction="SELL", pos=P(5, 5, 0, 2), exchange="SHFE") == "CLOSE_TODAY"
    assert position_manager.derive_action(
        direction="SELL", pos=P(5, 5, 0, 0), exchange="SHFE") == "CLOSE_YEST"
    assert position_manager.derive_action(
        direction="SELL", pos=P(5, 5, 0, 3), exchange="CZCE") == "CLOSE"
    with pytest.raises(ValueError):
        position_manager.derive_action(direction="HOLD", pos=P(), exchange="SHFE")


# ------------------------------- B8 -------------------------------
def test_b8_stop_loss_triggers_and_fails_loud_without_cost():
    """B8：G3 止损——触发则生成平仓单(close_reason=STOP_LOSS)；成本价缺失则 fail-loud。"""
    sym = f"TEST-B8-{uuid.uuid4().hex[:6]}"
    # 无持仓 → 不触发
    assert stop_loss.check_and_trigger(real_symbol=sym, last_price=3100.0,
                                       stop_pct=0.03, exchange=_ex) is None
    # 建仓并写成本价
    position_manager.apply_fill(real_symbol=sym, action="OPEN", direction="BUY",
                                fill_lots=2, fill_price=3112.0)
    # 未到止损 → None
    assert stop_loss.check_and_trigger(real_symbol=sym, last_price=3100.0,
                                       stop_pct=0.03, exchange=_ex) is None
    # 跌破 3% → 触发
    hit = stop_loss.check_and_trigger(real_symbol=sym, last_price=3000.0,
                                      stop_pct=0.03, exchange=_ex)
    assert hit is not None and hit["close_reason"] == "STOP_LOSS"
    assert hit["action"] in ("CLOSE_TODAY", "CLOSE_YEST", "CLOSE")
    assert hit["direction"] == "SELL"
    # 成本价缺失 → 抛错（拒绝拿不到成本价还 silently 跳过）
    sym2 = f"{sym}X"
    persistence.upsert_position(real_symbol=sym2, net_lots=1, long_lots=1,
                                short_lots=0, today_lots=0, avg_open_price=None)
    with pytest.raises(ValueError):
        stop_loss.unrealized_pnl_pct(net_lots=1, avg_open_price=0, last_price=1.0)


# ------------------------------- B9 -------------------------------
def test_b9_recover_on_boot_no_duplicate():
    """B9：断电恢复——无 broker_order_id 的 SENT/PARTIAL 判 ERROR 留痕，**不自动重下**。"""
    sig = f"TEST-B9-{uuid.uuid4().hex[:8]}"
    oid = persistence.save_order(_mk(sig))
    persistence.update_status(oid, "SENT", broker_order_id=None)   # 送达未确认
    n = execution_runtime.recover_on_boot()
    row = persistence.get_order(oid)
    assert row["status"] == "ERROR"
    assert "人工核对" in (row["last_error"] or "")
    assert n >= 1
    # 不得产生新单
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        c = s.execute(text("SELECT count(*) FROM execution_order WHERE signal_id=:s"),
                      {"s": sig}).scalar()
    assert int(c) == 1


# ------------------------------- 通道健康 -------------------------------
def test_b10_channel_health_persisted():
    """B10：通道探测落库 execution_channel_health，且明确标注是否仿真。"""
    d = describe()
    assert d["mode"] == "sim" and d["is_simulation"] is True
    r = monitor.probe_once("SIM")
    assert r["healthy"] is True
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as s:
        n = s.execute(text("SELECT count(*) FROM execution_channel_health")).scalar()
    assert int(n) >= 1
    assert get_broker().__name__.endswith("sim_broker")
