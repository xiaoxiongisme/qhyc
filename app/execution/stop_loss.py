# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · G3 止损执行（由 bar 事件驱动）。

流程：持仓 → 成本价算浮盈 → 触发则生成**平仓**单并写 ``close_reason='STOP_LOSS'``。

纪律
----
* **成本价缺失即抛错**（fail-loud）：avg_open_price 读不到时不猜、不静默跳过——
  拿不到成本价的止损等于没有止损。
* action 由 :func:`position_manager.derive_action` 推导（禁止用 direction 单独推断）。
* 本模块**只生成订单建议**并落库；真正下发由 :mod:`execution_runtime` 负责
  （保持「决策 / 执行」分离，便于 dry-run 与人工复核）。
"""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.execution import persistence, position_manager
from app.execution.broker import broker_mode


def unrealized_pnl_pct(*, net_lots: int, avg_open_price: float, last_price: float) -> float:
    """浮动盈亏比例（正=盈）。多头与空头方向已由 net_lots 的符号体现。"""
    if avg_open_price in (None, 0):
        raise ValueError("[fail-loud] avg_open_price 缺失/为 0，无法计算浮盈（拒绝猜测）")
    if net_lots > 0:
        return (last_price - avg_open_price) / avg_open_price
    return (avg_open_price - last_price) / avg_open_price


def check_and_trigger(
    *,
    real_symbol: str,
    last_price: float,
    stop_pct: float,
    exchange: str,
    symbol_continuous: str | None = None,
    signal_id: str | None = None,
    channel: str = "SIM",
    account: str | None = None,
) -> dict[str, Any] | None:
    """检查是否触发止损；触发则落一张平仓单并返回，未触发返回 ``None``。

    ``stop_pct`` 为正数阈值（如 0.03 = 亏 3% 止损）。
    """
    if stop_pct <= 0:
        raise ValueError(f"[fail-loud] stop_pct 必须为正数阈值，收到 {stop_pct!r}")

    pos = position_manager.load_position(real_symbol, channel=channel, account=account)
    if pos.is_flat:
        return None

    row = persistence.get_position(real_symbol, channel=channel, account=account)
    avg = (row or {}).get("avg_open_price")
    pnl = unrealized_pnl_pct(net_lots=pos.net_lots, avg_open_price=float(avg or 0.0),
                             last_price=float(last_price))

    if pnl > -stop_pct:
        return None

    action = position_manager.derive_action(direction="SELL" if pos.net_lots > 0 else "BUY",
                                            pos=pos, exchange=exchange)
    from datetime import date

    key = persistence.build_idempotency_key(
        signal_id or f"STOPLOSS:{real_symbol}", real_symbol, action, date.today())
    order = {
        "signal_id": signal_id or f"STOPLOSS:{real_symbol}:{last_price}",
        "symbol_continuous": symbol_continuous or f"{real_symbol[:-4]}888",
        "real_symbol": real_symbol,
        "exchange": exchange,
        "action": action,
        "direction": "SELL" if pos.net_lots > 0 else "BUY",
        "price": float(last_price),
        "price_space": "raw",
        "lots": abs(pos.net_lots),
        "channel": channel,
        "account": account,
        "idempotency_key": key,
    }
    order_id = persistence.save_order(order)
    logger.warning(
        f"[stop_loss] 触发止损 {real_symbol} 浮盈{pnl:.2%} ≤ -{stop_pct:.2%}"
        f"（cost={avg} last={last_price} lots={pos.net_lots}）→ 平仓单 {order_id} "
        f"action={action} broker={broker_mode()}")
    return {**order, "id": order_id, "unrealized_pnl_pct": pnl, "close_reason": "STOP_LOSS"}

