# -*- coding: utf-8 -*-
"""P0-1 组件④：点位校验（含盘口一致性安全闸）。

校验项（详见 PRD §3.4，已按 2026-10-02 实测数据模型修正）：
  1. 合约可交易：real_contract_metadata 可解析（futures_symbol 不含真实合约时回退连续 888）
  2. tick 对齐：price_tick 缺失则跳过并告警
  3. 盘口一致性（核心安全闸）：反解价 vs contract_daily 真实合约日频价偏差
  4. 涨跌停：数据源待接入，当前仅告警不阻断
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.symbol_code import to_native
from app.execution.roll_policy import real_contract_metadata

#: 实时盘口容差（默认 0.5%）
PRICE_TOL = float(__import__("os").getenv("PRICE_TOL", "0.005"))
#: 日频参照容差（默认 2%，因 contract_daily 为日线）
DAILY_TOL = float(__import__("os").getenv("DAILY_PRICE_TOL", "0.02"))
#: 硬阻断阈值（默认 5%，明显反解错误才阻断，避免日线噪声误杀）
BLOCK_TOL = float(__import__("os").getenv("PRICE_BLOCK_TOL", "0.05"))


def fetch_market_price(session: Session, real_symbol: str, when) -> tuple[Optional[float], Optional[str]]:
    """真实合约价取自 contract_daily（日频，原生大小写符号）。

    bar_*/hourly_bar 仅存 888 连续序列、无真实合约分钟/小时线，故一致性闸使用日频参照。
    返回 (price, granularity)；取不到返回 (None, None)。
    """
    d = when.date() if isinstance(when, datetime) else when
    try:
        from pandas import Timestamp
        if isinstance(when, Timestamp):
            d = when.date()
    except Exception:
        pass
    # contract_daily 中真实合约为原生大小写（SHFE/DCE 小写 rb2701，CZCE 大写 FG2701），
    # 须按交易所归一化后再查，否则 SHFE/DCE 会查不到盘口价。
    meta = real_contract_metadata(session, real_symbol)
    native = to_native(real_symbol, meta.get("exchange")) if meta.get("exchange") else real_symbol
    row = session.execute(
        text("SELECT close FROM contract_daily WHERE symbol=:s AND trade_date <= :d "
             "ORDER BY trade_date DESC LIMIT 1"),
        {"s": native, "d": d}).fetchone()
    if row is None or row[0] is None:
        return None, None
    return float(row[0]), "daily"


def check_tick(price: float, tick: Optional[float]) -> list[str]:
    warns: list[str] = []
    if tick is None or tick <= 0:
        return ["tick 未知，跳过对齐校验（futures_symbol.price_tick 待填充）"]
    aligned = abs(round(price / tick) * tick - price) <= 1e-6
    if not aligned:
        # 非阻断：下单前券商会拒绝非 tick 整数倍报单，这里仅提示
        warns.append(f"price={price} 非 tick={tick} 整数倍（下单前需取整）")
    return warns


def check_consistency(price: float, market: Optional[float], real_symbol: str,
                      granularity: Optional[str]) -> list[str]:
    """盘口一致性安全闸：偏差 > 硬阈值 → 阻断；> 软阈值 → 告警。"""
    if market is None:
        return [f"取不到 {real_symbol} 盘口价（contract_daily 无数据），跳过一致性校验（建议人工核对）"]
    dev = abs(price - market) / max(market, 1e-9)
    if dev > BLOCK_TOL:
        return [f"反解价 {price:.2f} 与盘口 {market:.2f} 偏差 {dev:.2%} "
                f"> 硬阈值 {BLOCK_TOL:.2%}：可能偏移/换月映射错误"]
    soft = DAILY_TOL if granularity == "daily" else PRICE_TOL
    if dev > soft:
        return [f"反解价 {price:.2f} 与盘口 {market:.2f} 偏差 {dev:.2%}（>容差 {soft:.2%}，建议核对）"]
    return []


def check_limit(session: Session, real_symbol: str, price: float, when) -> list[str]:
    """涨跌停校验：[待办] 接入涨跌停源后，price 超出 [limit_down, limit_up] 应阻断。

    当前数据源（daily_bar.limit_up/limit_down 等）未确认存在，先探测；
    无数据源则静默跳过（不阻断、不告警，避免误报）。
    """
    try:
        has = session.execute(
            text("SELECT 1 FROM information_schema.columns "
                 "WHERE table_name='daily_bar' AND column_name='limit_up' LIMIT 1")
        ).fetchone()
        if has is None:
            return []
    except Exception:
        return []
    # 数据源已存在但未接入比对逻辑：提示待补全（不阻断）
    return [f"涨跌停数据源疑似存在但未接入比对（{real_symbol}）"]


def validate(session: Session, real_symbol: str, price: float,
             when) -> tuple[list[str], list[str]]:
    """返回 (warnings, blocking_reasons)。"""
    warns: list[str] = []
    blocks: list[str] = []

    meta = real_contract_metadata(session, real_symbol)
    if meta["source"] == "missing":
        blocks.append(f"{real_symbol} 无元数据（futures_symbol 与连续 888 回退均缺失）")
    elif meta["source"] == "futures_symbol" and not meta["active"]:
        blocks.append(f"{real_symbol} 已标记 inactive")
    elif meta["source"] == "continuous_fallback":
        warns.append(f"{real_symbol} 元数据回退自连续 888（futures_symbol 未含真实合约）")

    warns += check_tick(price, meta["price_tick"])

    market, gran = fetch_market_price(session, real_symbol, when)
    blocks += check_consistency(price, market, real_symbol, gran)
    warns += check_limit(session, real_symbol, price, when)
    return warns, blocks
