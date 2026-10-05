# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · 进程内**仿真网关**（SimNow 等价物，零真实资金）。

用途
----
Sprint 1 目标是不接真实资金地跑通「下单 → 成交 → 持仓 → 状态机」全链路。本模块提供与
:mod:`app.execution.broker_http` **完全相同的三态接口**（submit / query_order / cancel / probe），
但订单簿在进程内内存中，按限价**立即全额成交**，因此：

* 可在无 CTP/QMT 行情与柜台的环境里跑 B0–B9 用例；
* 不会发出任何真实委托。

⚠ **绝不与真实资金混用**：本模块只在 ``EXECUTION_BROKER=sim``（默认）时被选中。

测试钩子（用于复现三态分支，默认全走正常路径）
------------------------------------------------
payload 带 ``_simulate`` 可强制分支：
  * ``"reject"``  → 返回 REJECT（验证 fail-loud 与 last_error 落库）
  * ``"timeout"`` → 返回 TIMEOUT_UNKNOWN（验证**禁止重试下单**、改走查单）
  * ``"partial"`` → 返回 PARTIAL（半仓，验证持仓按 fill_lots 滚动）
"""
from __future__ import annotations

import itertools
import threading
import time
from typing import Any

_lock = threading.Lock()
_seq = itertools.count(1)
_orders: dict[str, dict[str, Any]] = {}


def _next_id() -> str:
    return f"SIM{next(_seq):08d}"


def reset() -> None:
    """清空仿真订单簿并重置单号（测试隔离用）。"""
    global _seq
    with _lock:
        _orders.clear()
        _seq = itertools.count(1)


def submit(payload: dict[str, Any], *, retries: int = 0) -> dict[str, Any]:
    """提交订单。立即按限价成交（无滑点、无排队），返回三态 dict。"""
    sim = str(payload.get("_simulate") or "").lower()
    if sim == "timeout":
        return {"state": "TIMEOUT_UNKNOWN",
                "detail": "[sim] 强制模拟提交超时（未收到回执）"}
    if sim == "reject":
        return {"state": "REJECT", "error": "[sim] 强制模拟拒单"}

    bid = _next_id()
    price = float(payload["price"])
    lots = int(payload["lots"])
    fill_lots = max(1, lots // 2) if sim == "partial" else lots
    status = "PARTIAL" if sim == "partial" else "FILLED"
    rec = {
        "order_id": bid,
        "real_symbol": payload.get("real_symbol"),
        "direction": payload.get("direction"),
        "action": payload.get("action"),
        "price": price,
        "lots": lots,
        "status": status,
        "filled_lots": fill_lots,
        "avg_fill_price": price,
        "created_at": time.time(),
    }
    with _lock:
        _orders[bid] = rec
    return {"state": "ACK", "broker_order_id": bid,
            "filled_lots": fill_lots, "avg_fill_price": price, "raw": dict(rec)}


def query_order(broker_order_id: str) -> dict[str, Any]:
    """按 broker_order_id 查单。"""
    with _lock:
        rec = _orders.get(broker_order_id)
    if rec is None:
        return {"state": "NOT_FOUND"}
    return {"state": "OK", **rec}


def cancel(broker_order_id: str) -> dict[str, Any]:
    """撤单。已成交(SENT/PARTIAL/FILLED) 不可撤 → REJECT（与柜台语义一致）。"""
    with _lock:
        rec = _orders.get(broker_order_id)
        if rec is None:
            return {"state": "NOT_FOUND"}
        if rec["status"] != "NEW":
            return {"state": "REJECT", "error": f"[sim] 状态 {rec['status']} 不可撤"}
        rec["status"] = "CANCELED"
        return {"state": "OK", **rec}


def probe(channel: str = "SIM") -> dict[str, Any]:
    """健康探测：进程内仿真恒健康。"""
    return {"channel": channel, "healthy": True, "latency_ms": 0, "detail": "sim(in-process)"}
