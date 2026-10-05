# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · 执行编排与断电恢复。

状态机（DDL 030 CHECK 已固化第一层）：
    NEW --submit--> SENT --partial--> PARTIAL --fill--> FILLED
      └─ 任意态 ──> CANCELED | REJECTED | ERROR
    超时(TIMEOUT_UNKNOWN) → **禁止重试 submit**，先 query_order 判定

断电恢复：:func:`recover_on_boot` 是「进程启动时按 broker_order_id 回查未终态订单」的
**唯一入口**。缺它 → 重启后重复下单或丢单（真实资金事故）。
"""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.execution import persistence
from app.execution.broker import get_broker

#: 启动恢复时需回查的未终态
POLL_STATES = ("NEW", "SENT", "PARTIAL")

#: query_order 返回 → 库内状态的映射（网关侧终态为准）
_QUERY_STATE_MAP = {
    "FILLED": "FILLED",
    "PARTIAL": "PARTIAL",
    "CANCELED": "CANCELED",
    "REJECTED": "REJECTED",
    "NEW": "NEW",
    "SENT": "SENT",
}


def submit_order(order_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """把库内 NEW 订单提交到网关，按三态结果迁移状态。"""
    order = persistence.get_order(order_id)
    if order is None:
        raise ValueError(f"[fail-loud] 订单不存在 id={order_id}")
    if order["status"] != "NEW":
        # 幂等保护：非 NEW 不再提交（防重复下单的第二道闸）
        return {"state": "SKIPPED", "order_id": order_id,
                "reason": f"status={order['status']}"}
    if int(order.get("retry_count") or 0) > 0:
        # ★防重复下单的第三道闸：状态仍是 NEW，但**已发出过一次且未确认**（超时）。
        #   幂等键只护「落库」不护「提交」——若无此闸，再次 submit 会真的再下一次单。
        #   正确处置是走 reconcile_order 查单澄清，而非重下。
        return {"state": "SKIPPED", "order_id": order_id,
                "reason": f"已提交过未确认(retry_count={order['retry_count']})，"
                          f"禁止重试 submit，请走 reconcile_order 查单"}

    body = payload or {
        "real_symbol": order["real_symbol"],
        "direction": order["direction"],
        "action": order["action"],
        "price": float(order["price"]),
        "lots": int(order["lots"]),
        "channel": order["channel"],
        "account": order["account"],
    }
    resp = get_broker().submit(body)
    state = resp["state"]

    if state == "ACK":
        persistence.update_status(order_id, "SENT",
                                  broker_order_id=resp.get("broker_order_id"))
    elif state == "REJECT":
        persistence.update_status(order_id, "REJECTED",
                                  error=resp.get("error", "网关拒绝"))
    elif state == "TIMEOUT_UNKNOWN":
        # 关键：**不重试**、不改成 SENT（未确认送达），留 NEW 交由 recover_on_boot 查单；
        # 同时打上「已尝试」标记，机械阻断二次提交（防重复下单第三道闸）。
        persistence.mark_submit_attempt(order_id)
        logger.warning(
            f"[exec] 订单 {order_id} 提交超时（TIMEOUT_UNKNOWN），留 NEW 待查单，**禁止重试 submit**"
        )
    return {**resp, "order_id": order_id}


def reconcile_order(order_id: int) -> dict[str, Any]:
    """按 broker_order_id 查单并同步库内状态（超时澄清 / 轮询用）。

    ⚠ 回执里有两个「状态」：``state`` 是**回执信封**（OK / NOT_FOUND / REJECT…），
    **订单状态**在 ``status`` 字段（FILLED/PARTIAL/…）。曾误用 ``state`` 做映射，
    导致 mapped 恒为 None、状态机卡在 SENT——由 B2 用例抓出。
    """
    order = persistence.get_order(order_id)
    if order is None:
        raise ValueError(f"[fail-loud] 订单不存在 id={order_id}")
    bid = order.get("broker_order_id")
    if not bid:
        return {"state": "NO_BROKER_ID", "order_id": order_id,
                "detail": "无 broker_order_id，从未确认送达"}

    resp = get_broker().query_order(bid)

    if str(resp.get("state", "")).upper() == "NOT_FOUND":
        # 网关查无此单：可能从未送达或已过期。判 ERROR 留痕，**绝不静默重下**。
        persistence.update_status(
            order_id, "ERROR",
            error=f"网关查无此单 broker_order_id={bid}，无法确认送达，需人工核对（未自动重下）")
        return {"state": "NOT_FOUND", "order_id": order_id, "mapped": "ERROR"}

    raw = str(resp.get("status") or "").upper()
    if not raw:
        # 无订单状态字段 → 不猜、不改库
        return {"state": resp.get("state"), "order_id": order_id,
                "detail": "回执无 status 字段，无法判定（拒绝猜测）"}

    mapped = _QUERY_STATE_MAP.get(raw)
    if not mapped:
        return {"state": raw, "order_id": order_id, "detail": f"未知订单状态 {raw}，未改库"}
    if mapped != order["status"]:
        persistence.update_status(order_id, mapped, broker_order_id=bid)
    return {"state": raw, "order_id": order_id, "mapped": mapped}


def recover_on_boot() -> int:
    """进程启动时回查所有未终态订单，补齐状态。返回处理条数。

    无 broker_order_id 的 SENT/PARTIAL 判 ERROR 并留痕（需人工核对），**绝不静默重下**。
    """
    rows = persistence.list_open_orders(POLL_STATES)
    n = 0
    for r in rows:
        oid, bid, status = r["id"], r.get("broker_order_id"), r["status"]
        if not bid:
            if status in ("SENT", "PARTIAL"):
                persistence.update_status(
                    oid, "ERROR",
                    error="无 broker_order_id，重启后无法确认送达，需人工核对（未自动重下）",
                )
                n += 1
            continue
        try:
            reconcile_order(oid)
            n += 1
        except Exception as e:  # noqa: BLE001 —— 恢复期单条失败不阻断其余
            logger.error(f"[exec] recover_on_boot 订单 {oid} 回查失败：{e!r}")
            try:
                persistence.update_status(oid, "ERROR", error=f"重启回查失败：{e!r}")
            except Exception:  # noqa: BLE001 —— 状态已终态，忽略
                pass
    return n
