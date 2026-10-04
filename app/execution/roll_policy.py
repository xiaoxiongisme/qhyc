# -*- coding: utf-8 -*-
"""P0-1 组件②：换月规程（选真实合约 + 交割月护栏）+ 真实合约元数据解析。

实测数据模型（2026-10-02 探针，详见 PRD §9）：
- main_contract_map.main_symbol = 连续码（如 FG888）；**真实可交易合约在 underlying 列**（如 FG2701）。
- futures_symbol 仅含 73 个 888 连续码，**不含真实合约**；真实合约的 multiplier/exchange/
  price_tick 均为**品种级不变属性**，回退到同品种 888 连续码在语义上正确（非编造）。
- 真实合约日频价在 contract_daily（原生大小写符号），bar_*/hourly_bar 仅存 888 连续序列。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.symbol_code import delivery_ym, is_continuous, product_of
from app.models import MainContractMap
from app.repositories.symbol_repo import SymbolRepository

logger = logging.getLogger(__name__)

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


def _product_exchange(session, product):
    return session.execute(text(
        "SELECT exchange FROM dim_variety WHERE variety_code=:p"), {"p": product}).scalar()


def _resolve_underlying(session, product, trade_date):
    """参考主力（持仓量最大）口径：main_contract_map.underlying。丙/丁下仅作 WARN 对照。"""
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


def _resolve_inferred(session, product, trade_date):
    """丙/丁真源：888 当日实际所跟合约（价格匹配反推）。回退到 ≤trade_date 最近一条。"""
    return session.execute(text(
        "SELECT inferred_symbol FROM dim_main_contract_inferred "
        "WHERE variety_code=:p AND trade_date <= :d ORDER BY trade_date DESC LIMIT 1"),
        {"p": product, "d": trade_date}).scalar()


def _follow_rule(session, product):
    return session.execute(text(
        "SELECT continuous_follow_rule FROM dim_variety WHERE variety_code=:p"),
        {"p": product}).scalar()


def _warn_oi_rank(session, product, inferred, caliber, trade_date):
    """WB 修正增补：inferred 的 OI/近月排名不符合 follow_rule 时 WARN（补足 TRUE_MAIN 静默盲区）。"""
    if caliber not in ("NEAR_MONTH", "NEXT_MONTH", "TRUE_MAIN"):
        return
    rows = session.execute(text(
        "SELECT symbol, oi FROM contract_daily "
        "WHERE upper(left(symbol,:n))=:p AND trade_date=:d AND oi IS NOT NULL"),
        {"n": len(product), "p": product, "d": trade_date}).fetchall()
    if not rows:
        return

    def _ym(s):
        m = re.search(r"(\d{3,4})$", s)
        return int(m.group(1)) if m else None

    # 仅考虑「仍在交易」的合约：剔除已进入/过了交割月的合约
    # （如 RB2609 已交割、EG2610 进入交割月），否则会把已退市合约误当「近月」而误报。
    trade_yymm = trade_date.year % 100 * 100 + trade_date.month
    tradable = [(s, oi) for s, oi in rows if (_ym(s) or 0) > trade_yymm]
    if not tradable:
        return
    ranked = sorted([s for s, _ in tradable], key=lambda s: _ym(s) or 0)
    oi_max = max(tradable, key=lambda r: r[1])[0]
    if caliber == "NEAR_MONTH" and inferred != ranked[0]:
        logger.warning(f"[caliber OI] {product} NEAR_MONTH 预期近月 {ranked[0]}，"
                       f"实际 inferred {inferred}（{trade_date}）")
    elif caliber == "NEXT_MONTH" and len(ranked) > 1 and inferred != ranked[1]:
        logger.warning(f"[caliber OI] {product} NEXT_MONTH 预期次近月 {ranked[1]}，"
                       f"实际 inferred {inferred}（{trade_date}）")
    elif caliber == "TRUE_MAIN" and inferred != oi_max:
        logger.warning(f"[caliber OI] {product} TRUE_MAIN 预期 OI 最大 {oi_max}，"
                       f"实际 inferred {inferred}（{trade_date}）")


def select_real_contract(session: Session, symbol: str, trade_date: date):
    """返回 (real_symbol, exchange, change_flag, main_symbol)。

    丙/丁方案：真实合约优先取 dim_main_contract_inferred.inferred_symbol
    （888 当日实际所跟合约，价格匹配反推）；main_contract_map.underlying（持仓量最大）
    降为「参考主力」仅作 WARN 对照，不再 BLOCK。
    follow_rule=NULL（未标注）→ 按 G1 fail-loud 记录 ERROR（仍临时回退 underlying 避免崩溃，须补齐标注）。
    """
    if not is_continuous(symbol):
        return None, None, False, None
    product = product_of(symbol)
    if not product:
        return None, None, False, None

    caliber = _follow_rule(session, product)
    if caliber is None:
        logger.error(
            f"[caliber G1] {product} continuous_follow_rule 未标注（NULL），"
            f"执行层必须显式报错（fail-loud）；临时回退 underlying 仅避免崩溃，须补齐标注")

    inferred = _resolve_inferred(session, product, trade_date)
    underlying, exchange, change_flag, main_symbol = _resolve_underlying(session, product, trade_date)

    if inferred is not None:
        if underlying is not None and inferred != underlying:
            logger.warning(
                f"[caliber] {symbol} 反解 {inferred} ≠ 参考主力(underlying) {underlying}"
                f"（{trade_date}，caliber={caliber}）；丙/丁允许非最大持仓，仅告警不阻断")
        _warn_oi_rank(session, product, inferred, caliber, trade_date)
        ex = exchange or _product_exchange(session, product)
        return inferred, ex, change_flag, main_symbol

    if underlying is not None:
        logger.warning(
            f"[caliber] {symbol} 在 {trade_date} 无 inferred_symbol（dim_main_contract_inferred 空洞），"
            f"回退 underlying={underlying}（丙/丁预期按 inferred，请补全历史反推）")
        return underlying, exchange, change_flag, main_symbol
    return None, None, False, None


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
