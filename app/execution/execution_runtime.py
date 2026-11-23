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

from datetime import datetime, timezone
from decimal import Decimal
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


def _broker_fill_id(broker_order_id: str, fill_lots: int, fill_price: Decimal) -> str:
    """成交幂等键：同一笔 broker 单的同一「累计手数+均价」只记一次。

    与 ``execution_bridge.bridge_report`` 的合成口径一致，ON CONFLICT 由
    ``execution_fill(order_id, broker_fill_id)`` UNIQUE 兜底。部分成交后累计手数
    变化会生成新键，重复回报/重启回查则撞键不重复记账。
    """
    return f"{broker_order_id}#{fill_lots}@{fill_price}"


def _record_broker_fill(order: dict[str, Any], resp: dict[str, Any]) -> int:
    """把回执**自带**的成交明细写 execution_fill；返回累计成交手数（0=回执无明细）。

    CL-20261123-03：旧实现 ACK 只 ``update_status(SENT)``，sim_broker/CTP 回执里
    明明带了 ``filled_lots`` / ``avg_fill_price`` 却无人落库；``execution_fill``
    长期 0 行，滑点/成交率无法从真实成交计算。

    纪律：
      * 字段缺失，或显式 0 手/0 价（挂单未成交）→ 返回 0，不猜、不写脏账；
      * 只出现一个、或手数/价格非法 → **fail-loud**，不按 0/默认值兜底；
      * 只写 ``execution_fill``，不碰持仓（position_sync 默认 false，口径未变）。
    """
    lots_raw = resp.get("filled_lots")
    px_raw = resp.get("avg_fill_price")
    broker_id = resp.get("broker_order_id") or order.get("broker_order_id")
    if lots_raw is None and px_raw is None:
        return 0
    try:
        lots = int(lots_raw or 0)
        px = Decimal(str(px_raw)) if px_raw is not None else Decimal("0")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"[fail-loud] 成交回执字段类型非法：filled_lots={lots_raw!r}, "
            f"avg_fill_price={px_raw!r}") from exc
    # 显式「零成交」是正常形态：sim 限价挂单 ACK 回 0/None，CTP 未成交回报回 0/0。
    # 不能因为字段出现就当成交写库（否则 fill_lots=0 会被 CHECK 拒，或写出 0 价脏账）。
    if lots <= 0 and px <= 0:
        return 0
    if not broker_id:
        raise ValueError(
            "[fail-loud] 成交回执有成交明细但无 broker_order_id，无法生成幂等成交号")
    if lots <= 0 or px <= 0:
        raise ValueError(
            f"[fail-loud] 成交回执字段不完整/非法：filled_lots={lots_raw!r}, "
            f"avg_fill_price={px_raw!r}（拒绝按 0 兜底）")

    persistence.save_fill(
        int(order["id"]),
        broker_fill_id=_broker_fill_id(str(broker_id), lots, px),
        fill_ts=datetime.now(timezone.utc),
        fill_price=px,
        fill_lots=lots,
    )
    return lots


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
        # CL-20261121-05：价格类型必须下发，否则网关只能猜（现网 CTP 硬编码限价）。
        # 未改造的旧单（迁移前落库）取 DEFAULT 'LIMIT'，保持既有行为。
        "price_type": order.get("price_type") or "LIMIT",
        # CL-20261121-06：池级归属随 payload 下发，便于网关/审计侧识别同池单。
        "pool_id": order.get("pool_id"),
        "lots": int(order["lots"]),
        "channel": order["channel"],
        "account": order["account"],
    }
    resp = get_broker().submit(body)
    state = resp["state"]
    filled_saved = 0

    if state == "ACK":
        persistence.update_status(order_id, "SENT",
                                  broker_order_id=resp.get("broker_order_id"))
        # CL-20261123-03：ACK 回执自带成交明细（sim 立即成交 / 网关成交回报）必须落库。
        # 先 SENT 再记成交再终态：NEW→FILLED 直跳会被状态机拒绝；若 save_fill 失败，
        # 订单留在 SENT，recover_on_boot/reconcile 可重放，不会形成「状态终态但无成交」。
        filled_saved = _record_broker_fill(order, resp)
        if filled_saved:
            target = "FILLED" if filled_saved >= int(order["lots"]) else "PARTIAL"
            persistence.update_status(order_id, target,
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
    return {**resp, "order_id": order_id, "fill_lots_saved": filled_saved}


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

    # CL-20261123-03：查单回执是成交回报的第二入口（限价挂单 on_tick / CTP OnRtnTrade
    # 后重查、超时澄清、重启恢复都走这里）。即使状态已是 FILLED 也要重放 save_fill：
    # UNIQUE(order_id, broker_fill_id) 保证重复回查不重复记账，却能补回上次落库失败的明细。
    fill_lots_saved = 0
    if mapped in ("FILLED", "PARTIAL"):
        fill_lots_saved = _record_broker_fill(order, resp)
    if mapped != order["status"]:
        persistence.update_status(order_id, mapped, broker_order_id=bid)
    return {"state": raw, "order_id": order_id, "mapped": mapped,
            "fill_lots_saved": fill_lots_saved}


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
            except Exception as exc:  # noqa: BLE001 —— 状态已终态，忽略
                logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")
    return n
