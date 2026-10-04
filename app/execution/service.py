# -*- coding: utf-8 -*-
"""P0-1 编排层：信号 → 可执行订单建议。

入口 resolve_signal_to_order(session, req) -> ExecutionOrder
流程见 PRD §4。任一阻断项 → blocking_reasons 非空、lots=0、禁止下发。

实测数据模型修正（2026-10-02 探针）：
- 真实合约取 main_contract_map.underlying（非 main_symbol）。
- 真实合约元数据（multiplier 等）通过 real_contract_metadata 回退连续 888。
- 价空间默认 raw（888 序列为原始连续价），price_space="adj" 才减偏移。
- 盘口一致性闸源 = contract_daily（真实合约日频价）。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from app.execution.lot_sizing import from_target_risk, validate_given_lots
from app.execution.reverse_price import reverse_price
from app.execution.roll_policy import (
    active_guard,
    delivery_guard,
    real_contract_metadata,
    select_real_contract,
)
from app.execution.schemas import ExecutionOrder, ReverseRequest
from app.execution.validator import validate


class ReverseResolutionError(Exception):
    """反解层无法继续（入参非法等）。"""


def _when(req: ReverseRequest) -> datetime:
    if req.trade_datetime is not None:
        return pd.Timestamp(req.trade_datetime).to_pydatetime()
    # 缺省：交易日 15:00（日盘收盘）作为 as-of 时点
    return pd.Timestamp(req.trade_date).to_pydatetime()


def resolve_signal_to_order(session: Session, req: ReverseRequest) -> ExecutionOrder:
    order = ExecutionOrder(direction=req.direction)

    # ---- 守卫 ----
    if req.direction == "FLAT":
        order.warnings.append("FLAT 信号，不下新单")
        return order
    if not req.symbol.endswith("888"):
        order.blocking_reasons.append(f"{req.symbol} 非连续主力码（须为 <品种>888）")
        return order

    when = _when(req)
    order.meta["when"] = str(when)

    # ---- ② 换月：选真实合约（underlying）+ 交割月护栏 ----
    real_symbol, exchange, change_flag, main_symbol = select_real_contract(
        session, req.symbol, req.trade_date)
    if real_symbol is None:
        order.blocking_reasons.append(
            f"无法解析 {req.symbol} 在 {req.trade_date} 的真实主力合约")
        return order
    order.real_symbol = real_symbol
    order.exchange = exchange
    order.product = req.symbol[:-3]
    order.meta["main_symbol"] = main_symbol
    if change_flag:
        order.warnings.append("今日主力切换（换月），建议人工确认")
    order.blocking_reasons += delivery_guard(real_symbol, req.trade_date)
    order.blocking_reasons += active_guard(session, real_symbol)

    # ---- ① 价格反解（price_space 决定偏移是否生效）----
    if req.entry_px is not None:
        space = req.price_space
        real_entry, cum = reverse_price(session, req.symbol, req.entry_px, when, space)
        order.price = real_entry
        order.meta["cum_offset"] = cum
        order.meta["price_space"] = space or "raw"
        if req.stop_px is not None:
            real_stop, _ = reverse_price(session, req.symbol, req.stop_px, when, space)
            order.stop_price = real_stop

    # ---- ③ 手数（元数据回退连续 888）----
    meta = real_contract_metadata(session, real_symbol)
    multiplier = meta["multiplier"]
    order.multiplier = multiplier
    if multiplier is None:
        order.warnings.append(f"{real_symbol} 取不到 multiplier（回退失败）")
    if req.target_risk is not None and req.atr:
        lots, warns = from_target_risk(req.target_risk, req.atr, multiplier or 0.0,
                                       order.price or 0.0)
        order.warnings += warns
    elif req.lots is not None:
        lots, warns = validate_given_lots(req.lots)
        order.warnings += warns
    else:
        lots, warns = 0, ["未提供 lots 也未提供 target_risk，无法定手数"]
        order.warnings += warns
        order.blocking_reasons.append("缺失手数来源")
    order.lots = lots
    if multiplier and order.price:
        order.notional = lots * multiplier * order.price

    # ---- ④ 点位校验（含盘口一致性安全闸，源=contract_daily，仅 raw 空间生效）----
    if order.price is not None and real_symbol:
        v_warns, v_blocks = validate(session, real_symbol, order.price, when, space or "raw")
        order.warnings += v_warns
        order.blocking_reasons += v_blocks

    return order
