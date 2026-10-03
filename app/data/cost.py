# -*- coding: utf-8 -*-
"""交易成本解析：唯一权威 = 库表 ``dim_trading_cost``（迁移 013/014）。

为什么需要
----------
回测与执行此前各用各的成本口径（``cost_pct`` 百分比、``fee_per_lot`` 死数据、多份
``SPEC`` 副本），彼此不可比；且费率实际是「三动作 × 两类型 × 合约范围」的组合，
无法用一个标量表达。本模块把复杂度收进库表，调用方只问「这次成交多少钱」。

口径（用户拍板）
----------------
* 实际成本 = **交易所标准 + 券商加收 1 分/手**，滑点 **1 跳/边**（014 迁移）。
* 三动作不可合并：OPEN / CLOSE_YEST / CLOSE_TODAY（苹果开仓 5、平今 20）。
* 两类费率：FIXED(元/手) 与 PCT(**‱ 万分之一**，按成交额)。调用方不必判断量纲。
  ⚠ 单位裁定（用户 2026-10-03 拍板，取「B」）：交易所通知里的比例值原文写作「N%」，
    其含义是**万分之 N**，故 ``PCT`` 的 ``fee_value`` 以 **‱(1/10000)** 为单位，
    数值与通知原文 **1:1 对应**（通知「1%」→ 1.0‱）以便审计与换版。
    历史教训：曾按千分之(‰)解释，导致 BZ 纯苯成本高估 10 倍
    （186,930×0.001=186.93 元/边，而正确值为 18.69 元/边）；
    akshare 的费率表亦按 ‰ 记（RB 6.07 实为 ‰ 口径），已判定其比例值不可用。
* 合约范围：交易所对特定合约给不同费率（碳酸锂 0.8‱，2601~2702 为 3.2‱），
  按 CONTRACTS > MONTHS > ALL 取最具体的一条。

**未知即报错**：查不到抛 :class:`CostNotFoundError`，绝不按 0 成本静默计算
——把「未知」当「免费」会系统性低估回测成本。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import text

from app.core.db import session_scope

#: PCT 费率值的单位换算除数：万分之一（‱）。
#: ⚠ 改动此值会全局改变所有按成交额计费的回测/实盘成本，务必同步迁移 019。
PCT_DIVISOR = 10000.0

__all__ = ["TradingCost", "CostNotFoundError", "fee_per_lot",
           "fee_yuan_per_lot", "round_trip_cost", "PCT_DIVISOR"]

_SCOPE_RANK = {"CONTRACTS": 3, "MONTHS": 2, "ALL": 1}


class CostNotFoundError(LookupError):
    """费率不存在或范围不匹配 —— 必须报错，禁止按 0 计。"""


@dataclass(frozen=True)
class TradingCost:
    variety_code: str
    action: str
    fee_type: str
    fee_value: float
    exchange_fee_yuan: float
    broker_markup_type: str
    broker_markup_yuan: float
    slip_ticks: float
    scope_kind: str
    effective_from: date
    source: str
    note: Optional[str] = None
    #: 本次取费率的**口径日**（= 传入的 as_of，或降级时的当期）
    fee_as_of: Optional[date] = None
    #: True 表示该 as_of 无历史费率、已按用户裁定退到**当期**费率。
    #: ⚠ 必须可观测 —— 静默降级是本项目最主要的缺陷类型。
    fell_back_to_current: bool = False

    @property
    def fee_yuan(self) -> float:
        return self.exchange_fee_yuan + self.broker_markup_yuan


def _variety(symbol: str) -> str:
    from app.data.barstore import variety_of
    vc = variety_of(symbol)
    if not vc:
        raise CostNotFoundError(f"cannot resolve variety from {symbol!r}")
    return vc.upper()


def _contract_parts(contract):
    if not contract:
        return None, None
    from app.core.symbol_code import to_std
    s = to_std(contract)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 4:
        return None, None
    return [digits[-4:]], int(digits[-2:])


def fee_per_lot(symbol, action, *, contract=None, on_date=None, kind="FUTURE"):
    """取「一次成交(一手)」的成本要素。未知即抛 CostNotFoundError。

    :param action: OPEN / CLOSE_YEST / CLOSE_TODAY
    :param contract: 具体合约码，用于命中「特定合约」费率；None 则只按全范围
    :param on_date: 费率生效日，默认今天。**回测历史务必显式传入** ——
        否则会用今天的费率算历史（滚动表的意义就在这里）。
    """
    action = action.upper()
    if action not in ("OPEN", "CLOSE_YEST", "CLOSE_TODAY"):
        raise ValueError(f"bad action {action!r}")
    vc = _variety(symbol)
    d = on_date or date.today()
    c4, cmonth = _contract_parts(contract)

    with session_scope() as s:
        rows = s.execute(text(
            "SELECT scope_kind, scope_months, scope_contracts, fee_type, fee_value, "
            "       exchange_fee_value, broker_markup_type, broker_markup_value, "
            "       slip_ticks, effective_from, source, note "
            "FROM dim_trading_cost "
            "WHERE upper(variety_code) = :vc AND instrument_kind = :kind AND action = :act "
            "  AND effective_from <= :d AND (effective_to IS NULL OR effective_to > :d)"),
            {"vc": vc, "kind": kind, "act": action, "d": d}).fetchall()

    if not rows:
        # ── 用户 2026-10-03 裁定：「没有历史费率就按当前费率计算」────────────────
        # 费率表最早 effective_from = 2026-03-11（交易所通知日），更早的回测无历史行。
        # 口径选择：退到**当期有效行**，而不是报错。
        # ⚠ 但降级必须**可观测**（返回对象带 fee_as_of / fell_back_to_current），
        #   否则就成了本项目最典型的「静默失效」陷阱（见 memory: 静默失效是主要缺陷类型）。
        cur = s.execute(text(
            "SELECT scope_kind, scope_months, scope_contracts, fee_type, fee_value, "
            "       exchange_fee_value, broker_markup_type, broker_markup_value, "
            "       slip_ticks, effective_from, source, note "
            "FROM dim_trading_cost "
            "WHERE upper(variety_code) = :vc AND instrument_kind = :kind AND action = :act "
            "  AND effective_to IS NULL"),
            {"vc": vc, "kind": kind, "act": action}).fetchall()
        if not cur:
            raise CostNotFoundError(
                f"{vc}/{action}: dim_trading_cost 中完全无该动作费率（{d}）。"
                f"拒绝按 0 成本计算 —— 请补费率来源后重跑 "
                f"scripts/sync_cost_from_exchange.py --apply")
        rows, fell_back = cur, True
    else:
        fell_back = False

    best = None
    for r in rows:
        sk = r[0]
        if sk == "CONTRACTS":
            lst = [x.upper() for x in (r[2] or [])]
            if not (c4 and any(x.endswith(tuple(c4)) for x in lst)):
                continue
        elif sk == "MONTHS":
            if cmonth is None or cmonth not in (r[1] or []):
                continue
        rank = _SCOPE_RANK.get(sk, 0)
        if best is None or rank > best[0]:
            best = (rank, r)

    if best is None:
        raise CostNotFoundError(
            f"{vc}/{action} at {d}: fee rows exist but none applies to contract={contract!r}. "
            f"REFUSING to compute as 0 cost.")

    r = best[1]
    ex_src = r[5] if r[5] is not None else r[4]
    return TradingCost(
        variety_code=vc, action=action, fee_type=r[3], fee_value=float(r[4] or 0),
        exchange_fee_yuan=float(ex_src or 0),
        broker_markup_type=r[6] or "NONE",
        broker_markup_yuan=(float(r[7] or 0) if (r[6] or "NONE") in ("FIXED", "PCT") else 0.0),
        slip_ticks=float(r[8] or 0), scope_kind=r[0], effective_from=r[9],
        source=r[10], note=r[11],
        fee_as_of=d, fell_back_to_current=fell_back)


def fee_yuan_per_lot(symbol, action, price, *, contract=None, on_date=None, kind="FUTURE"):
    """折算成「元/手」。PCT 按成交额(price x multiplier) x ‱；FIXED 直接取值。"""
    from app.data.barstore import variety_spec
    tc = fee_per_lot(symbol, action, contract=contract, on_date=on_date, kind=kind)
    if tc.fee_type == "FREE":
        ex = 0.0
    elif tc.fee_type == "PCT":
        mult = variety_spec(symbol)["multiplier"]
        ex = float(price) * mult * tc.exchange_fee_yuan / PCT_DIVISOR
    else:
        ex = tc.exchange_fee_yuan
    markup = tc.broker_markup_yuan
    if tc.broker_markup_type == "PCT":
        mult = variety_spec(symbol)["multiplier"]
        markup = float(price) * mult * tc.broker_markup_yuan / PCT_DIVISOR
    return ex + markup


def cost_coefficients(symbol, *, contract=None, on_date=None, kind="FUTURE",
                      close_action="CLOSE_YEST") -> dict:
    """把一次开平成本分解为「固定项 + 按价格比例项 + 滑点」，供回测逐笔高效求值。

    回测里每笔交易都要算成本，逐笔查库不可接受；而费率只有两种形态
    （固定值 / 按成交额‱），故可一次解析、之后按价格闭式求值：

        cost_yuan(price) = fixed_yuan
                        + price x multiplier x pct_rate / 10000
                        + slip_yuan

    滑点为**每边** ``slip_ticks x tick_size x multiplier``，开平两次故乘 2。
    """
    from app.data.barstore import variety_spec
    sp = variety_spec(symbol)
    tick, mult = sp.get("tick_size"), sp["multiplier"]

    o = fee_per_lot(symbol, "OPEN", contract=contract, on_date=on_date, kind=kind)
    if not close_action_available(symbol, close_action, contract=contract,
                                  on_date=on_date, kind=kind):
        if close_action != "CLOSE_YEST":
            close_action = "CLOSE_YEST"
        if not close_action_available(symbol, close_action, contract=contract,
                                      on_date=on_date, kind=kind):
            raise CostNotFoundError(f"{sp['variety_code']}: 开仓与平昨费率均缺失")

    fixed_yuan = 0.0
    pct_rate = 0.0
    for act in ("OPEN", close_action):
        tc = fee_per_lot(symbol, act, contract=contract, on_date=on_date, kind=kind)
        if tc.fee_type == "PCT":
            pct_rate += tc.exchange_fee_yuan
        elif tc.fee_type == "FIXED":
            fixed_yuan += tc.exchange_fee_yuan
        # FREE = 0
        if tc.broker_markup_type == "FIXED":
            fixed_yuan += tc.broker_markup_yuan
        elif tc.broker_markup_type == "PCT":
            pct_rate += tc.broker_markup_yuan
    slip_yuan = 2.0 * (o.slip_ticks or 0) * (tick or 0) * mult
    return {
        "variety_code": sp["variety_code"],
        "multiplier": mult,
        "tick_size": tick,
        "fixed_yuan": fixed_yuan,
        "pct_rate": pct_rate,
        "slip_yuan": slip_yuan,
        "slip_ticks_per_side": o.slip_ticks,
        "close_action": close_action,
        "open_scope": o.scope_kind,
    }


def cost_yuan_at(coefficients: dict, price: float) -> float:
    """按 ``cost_coefficients`` 给定系数求某个成交价的一次开平成本（元/手）。"""
    c = coefficients
    return (c["fixed_yuan"]
            + float(price) * c["multiplier"] * c["pct_rate"] / PCT_DIVISOR
            + c["slip_yuan"])


def cost_points_at(coefficients: dict, price: float) -> float:
    """同上，但折成**点数**（除以乘数），以便与回测的点数口径 pnl 相减。"""
    m = coefficients["multiplier"]
    return cost_yuan_at(coefficients, price) / m if m else float("nan")


def close_action_available(symbol, action, *, contract=None, on_date=None,
                           kind="FUTURE") -> bool:
    """该动作是否有费率（供回测降级判断，不抛错）。"""
    try:
        fee_per_lot(symbol, action, contract=contract, on_date=on_date, kind=kind)
        return True
    except CostNotFoundError:
        return False


def round_trip_cost(symbol, price, *, contract=None, on_date=None, lots=1,
                    kind="FUTURE", close_action="CLOSE_YEST", strict=False):
    """一次完整开平的总成本（元）。

    组成：开仓费 + 平仓费 + 滑点（**每边 1 跳**，故开平各一次 = 2 x slip_ticks x
    tick_size x multiplier x 手数）。

    :param close_action: 默认 ``CLOSE_YEST``。**注意**：手续费来源文件对多数品种
        **未给平今费率**（当前 166 个动作里仅 81 个有 CLOSE_TODAY），若强用平今会在
        回测中大面积报「费率未知」。K 线级回测按「隔日平仓」建模更贴近实际，故默认
        平昨；确需当日平仓时显式传 ``close_action="CLOSE_TODAY"``。
    :param strict: True 时缺该动作费率即抛错（实盘校验用，不接受任何降级）。
    """
    from app.data.barstore import variety_spec
    sp = variety_spec(symbol)
    tick, mult = sp.get("tick_size"), sp["multiplier"]
    o = fee_per_lot(symbol, "OPEN", contract=contract, on_date=on_date, kind=kind)

    if not close_action_available(symbol, close_action, contract=contract,
                                  on_date=on_date, kind=kind):
        if strict or close_action == "CLOSE_YEST":
            raise CostNotFoundError(
                f"{sp['variety_code']}: 平仓动作 {close_action} 无费率"
                f"{'（strict=True，不接受降级）' if strict else ''}")
        close_action = "CLOSE_YEST"
        if not close_action_available(symbol, close_action, contract=contract,
                                      on_date=on_date, kind=kind):
            raise CostNotFoundError(
                f"{sp['variety_code']}: 连平昨费率也缺失，无法计算成本")

    fee_open = fee_yuan_per_lot(symbol, "OPEN", price, contract=contract,
                                on_date=on_date, kind=kind)
    fee_close = fee_yuan_per_lot(symbol, close_action, price, contract=contract,
                                 on_date=on_date, kind=kind)
    slip_per_side = (o.slip_ticks or 0) * (tick or 0) * mult
    return {
        "fee_open": fee_open * lots,
        "fee_close": fee_close * lots,
        "slippage": 2 * slip_per_side * lots,
        "total": (fee_open + fee_close + 2 * slip_per_side) * lots,
        "open_scope": o.scope_kind,
        "close_action": close_action,
        "slip_ticks_per_side": o.slip_ticks,
        "tick_size": tick,
        "multiplier": mult,
    }
