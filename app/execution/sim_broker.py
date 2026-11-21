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

限价挂单生命周期（CL-20261121-05 / 5M-P0B）
------------------------------------------------
默认仍按限价**立即全额成交**，B0–B10 行为逐位不变。仅当 payload 显式带
``queue_limit=True`` 且 ``price_type=LIMIT`` 时进入「挂单模式」：submit 只挂
NEW 单，:func:`on_tick` 在行情触及限价时才成交，:func:`cancel` 可撤未成交单。
这样模拟盘才能测出「不成交/排队/撤单重挂」，而不是永远假装成交。
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
    ptype = str(payload.get("price_type") or "LIMIT").upper()
    # 显式 opt-in 才排队：默认立即成交，既有 B0–B10 语义不变。
    queueing = bool(payload.get("queue_limit")) and ptype == "LIMIT" and not sim
    if queueing:
        fill_lots, status, avg_px = 0, "NEW", None
    else:
        fill_lots = max(1, lots // 2) if sim == "partial" else lots
        status = "PARTIAL" if sim == "partial" else "FILLED"
        avg_px = price
    rec = {
        "order_id": bid,
        "real_symbol": payload.get("real_symbol"),
        "direction": payload.get("direction"),
        "action": payload.get("action"),
        "price": price,
        "price_type": ptype,
        "queueing": queueing,
        "lots": lots,
        "status": status,
        "filled_lots": fill_lots,
        "avg_fill_price": avg_px,
        "created_at": time.time(),
    }
    with _lock:
        _orders[bid] = rec
    return {"state": "ACK", "broker_order_id": bid,
            "filled_lots": fill_lots, "avg_fill_price": avg_px, "raw": dict(rec)}


def query_order(broker_order_id: str) -> dict[str, Any]:
    """按 broker_order_id 查单。"""
    with _lock:
        rec = _orders.get(broker_order_id)
    if rec is None:
        return {"state": "NOT_FOUND"}
    return {"state": "OK", **rec}


def on_tick(broker_order_id: str, market_price: float) -> dict[str, Any]:
    """行情推进一步：只让「已挂出的限价单」在价格触及限价时成交。

    成交价取对客户更有价的一侧（跳空穿越限价时不亏于限价），与回测里
    「限价触及即成交」的乐观口径一致；未触及则保持 NEW，供上层撤单/重挂。
    """
    with _lock:
        rec = _orders.get(broker_order_id)
        if rec is None:
            return {"state": "NOT_FOUND"}
        if rec["status"] != "NEW" or not rec.get("queueing"):
            return {"state": "OK", "status": rec["status"], "note": "非挂单状态，忽略 tick"}
        limit = float(rec["price"])
        px = float(market_price)
        # 买限价：市价跌到 ≤ 限价才成交；卖限价：市价涨到 ≥ 限价才成交。
        touched = (px >= limit) if str(rec.get("direction", "")).upper() == "SELL" else (px <= limit)
        if not touched:
            return {"state": "OK", "status": "NEW", "filled_lots": 0,
                    "note": "未触及限价，继续排队"}
        # 用更严格的一侧判断触及，但成交价取更有利价（限价保护）。
        fill_px = max(limit, px) if str(rec.get("direction", "")).upper() == "SELL" else min(limit, px)
        rec["status"] = "FILLED"
        rec["filled_lots"] = int(rec["lots"])
        rec["avg_fill_price"] = fill_px
        return {"state": "OK", **rec}


def cancel(broker_order_id: str) -> dict[str, Any]:
    """撤单。已成交(SENT/PARTIAL/FILLED) 不可撤 → REJECT（与柜台语义一致）。
    挂单模式下 NEW 单可撤（CL-20261121-05）。"""
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
