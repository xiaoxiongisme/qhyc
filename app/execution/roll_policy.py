# -*- coding: utf-8 -*-
"""P0-1 组件②：换月规程（选真实合约 + 交割月护栏）+ 真实合约元数据解析。

实测数据模型（2026-10-02 探针，详见 PRD §9）：
- main_contract_map.main_symbol = 连续码（如 FG888）；**真实可交易合约在 underlying 列**（如 FG2701）。
- futures_symbol 仅含 73 个 888 连续码，**不含真实合约**；真实合约的 multiplier/exchange/
  price_tick 均为**品种级不变属性**，回退到同品种 888 连续码在语义上正确（非编造）。
- 真实合约日频价在 contract_daily（原生大小写符号），bar_*/hourly_bar 仅存 888 连续序列。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.symbol_code import delivery_ym, is_continuous, product_of
from app.models import MainContractMap
from app.repositories.symbol_repo import SymbolRepository

#: 距最后交易日多少自然日内禁止新开（可配：环境变量 ROLL_BUFFER_DAYS）
ROLL_BUFFER_DAYS = int(__import__("os").getenv("ROLL_BUFFER_DAYS", "5"))


def _last_trading_day(year: int, month: int) -> date:
    """交割月第 10 个交易日（简化：跳过周末，忽略节假日；偏保守不影响护栏可靠性）。"""
    d = date(year, month, 1)
    count = 0
    while count < 10:
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            count += 1
        if count < 10:
            d = d + timedelta(days=1)
    return d


def select_real_contract(session: Session, symbol: str, trade_date: date):
    """返回 (real_symbol, exchange, change_flag, main_symbol)。

    real_symbol 取 main_contract_map.underlying（真实可交易合约，如 FG2701）；
    main_symbol 为连续码（如 FG888），仅作引用。查不到当日映射则回退最近 ≤ trade_date 的映射。
    """
    if not is_continuous(symbol):
        return None, None, False, None
    product = product_of(symbol)
    if not product:
        return None, None, False, None
    from app.repositories.main_contract_repo import MainContractRepository
    repo = MainContractRepository(session)
    rows = repo.query(product=product, start=trade_date, end=trade_date)
    if not rows:
        recent = (session.query(MainContractMap)
                  .filter_by(product=product)
                  .filter(MainContractMap.trade_date <= trade_date)
                  .order_by(MainContractMap.trade_date.desc())
                  .first())
        if recent is None:
            return None, None, False, None
        rows = [recent]
    row = rows[0]
    return row.underlying, row.exchange, bool(row.change_flag), row.main_symbol


def real_contract_metadata(session: Session, real_symbol: str) -> dict:
    """解析真实合约元数据；futures_symbol 仅含 888 连续码时回退到同品种 888。

    返回 {exchange, multiplier, price_tick, active, source}；
    source ∈ {futures_symbol, continuous_fallback, missing}。
    multiplier/exchange/price_tick 为品种级不变属性，888 连续码即代表该品种，
    故回退在语义上正确（非编造）。
    """
    sym = SymbolRepository(session).get(real_symbol)
    if sym is not None:
        return {
            "exchange": sym.exchange,
            "multiplier": float(sym.multiplier) if sym.multiplier is not None else None,
            "price_tick": float(sym.price_tick) if sym.price_tick is not None else None,
            "active": bool(sym.active),
            "source": "futures_symbol",
        }
    prod = product_of(real_symbol)
    cont = SymbolRepository(session).get(f"{prod}888") if prod else None
    if cont is not None:
        return {
            "exchange": cont.exchange,
            "multiplier": float(cont.multiplier) if cont.multiplier is not None else None,
            "price_tick": float(cont.price_tick) if cont.price_tick is not None else None,
            "active": True,
            "source": "continuous_fallback",
        }
    return {"exchange": None, "multiplier": None, "price_tick": None,
            "active": True, "source": "missing"}


def delivery_guard(real_symbol: str, trade_date: date) -> list[str]:
    """交割月护栏：返回阻断原因列表（空=通过）。"""
    reasons: list[str] = []
    yy, mm = delivery_ym(real_symbol)
    if yy is None or mm is None:
        reasons.append(f"无法解析交割月: {real_symbol}")
        return reasons
    ltd = _last_trading_day(yy, mm)
    if (ltd - trade_date).days <= ROLL_BUFFER_DAYS:
        reasons.append(
            f"{real_symbol} 距最后交易日 {ltd} 仅 {(ltd - trade_date).days} 天"
            f"(≤{ROLL_BUFFER_DAYS})，禁止新开"
        )
    return reasons


def active_guard(session: Session, real_symbol: str) -> list[str]:
    """活跃性校验（基于 real_contract_metadata 的统一口径）。"""
    meta = real_contract_metadata(session, real_symbol)
    if meta["source"] == "missing":
        return [f"{real_symbol} 无元数据（futures_symbol 与连续 888 回退均缺失）"]
    if meta["source"] == "futures_symbol" and not meta["active"]:
        return [f"{real_symbol} 已标记 inactive"]
    return []
