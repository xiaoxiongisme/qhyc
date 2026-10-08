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

from app.core.db import get_engine, session_scope

#: PCT 费率值的单位换算除数：万分之一（‱）。
#: ⚠ 改动此值会全局改变所有按成交额计费的回测/实盘成本，务必同步迁移 019。
PCT_DIVISOR = 10000.0

__all__ = ["TradingCost", "CostNotFoundError", "fee_per_lot",
           "fee_yuan_per_lot", "round_trip_cost", "PCT_DIVISOR",
           "clear_fee_cache", "fee_cache_stats", "reset_fee_cache_stats"]

_SCOPE_RANK = {"CONTRACTS": 3, "MONTHS": 2, "ALL": 1}

# ---------------------------------------------------------------------------
# 费率查询缓存（2026-10-06）
# ---------------------------------------------------------------------------
#: 为什么需要：费率表 dim_trading_cost 是**低频变更的参照数据**，但回测里
#: ``cost_coefficients`` / ``fee_yuan_per_lot`` 会被逐笔调用十万量级，且每次都
#: 至少 1~2 条 SQL（历史日期必走 fallback → 同一 action 查两次）。
#: 不缓存 = 回测全程把连接池当查询接口用，必然成为最慢的一环。
#:
#: 为什么安全：键是 (variety_code, action, contract, on_date, kind)，且只在
#: **单次查询**结果上缓存——``dim_trading_cost`` 的行在一次运行内不变；
#: 费率字典重载后必须调 :func:`clear_fee_cache`（``load_cost_dict.py`` 已调用）。
#:
#: ⚠ 不可缓存的是「取不到时抛 CostNotFoundError」的结果**之外**的任何状态；
#:   异常不进缓存，避免一次误判被永久固化。
_FEE_CACHE: dict = {}
_FEE_CACHE_MAX = 8192

#: 费率查询可观测性计数（2026-10-08 工单 P2-1）。
#:
#: 为什么加：这次「回测异常慢」定位极耗时——工单实测 NullPool 下单次费率查询
#: 0.303s、单品种 simulate 18.6s，最后是从 profile 里抠出来的。**光看耗时无法区分
#: 是连接层（冷连接）还是查询层（缓存 miss / fallback 慢）**。有了这组计数，
#: 下次 5 分钟可定性：
#:   * ``miss`` 高、`hit`` 低 → 键设计有问题（如按成交日而非费率窗口归约）→ 查键
#:   * ``miss`` 低但仍慢        → 纯连接层问题（NullPool / 隧道）→ 查池配置
#:   * ``fallback`` 占比高      → 费率表覆盖不足，查询退化为当期行扫描
_FEE_STATS = {"hit": 0, "miss": 0, "fallback": 0, "evict": 0, "error": 0}


def fee_cache_stats() -> dict:
    """返回费率查询缓存计数（诊断用，不影响业务）。

    键：``hit`` 命中 / ``miss`` 未命中 / ``fallback`` 走了「无匹配→取当期行」分支 /
    ``evict`` 因超上限清空 / ``error`` 查询抛错。``size`` 为当前缓存条目数。
    """
    total = _FEE_STATS["hit"] + _FEE_STATS["miss"]
    out = dict(_FEE_STATS)
    out["size"] = len(_FEE_CACHE)
    out["hit_rate"] = (out["hit"] / total) if total else 0.0
    return out


def reset_fee_cache_stats() -> None:
    """清零计数（压测/基准脚本前后调用，避免上一轮污染读数）。"""
    for k in _FEE_STATS:
        _FEE_STATS[k] = 0


def clear_fee_cache() -> None:
    """清空费率查询缓存（费率字典重载 / 迁移后必须调用，否则读到旧费率）。"""
    _FEE_CACHE.clear()
    _RATE_WINDOWS.clear()
    _MAIN_CONTRACT.clear()


# ---------------------------------------------------------------------------
# 费率变动窗口（2026-10-07）
# ---------------------------------------------------------------------------
#: 为什么要「按窗口归约日期」：
#:   历史回测逐笔取费率时，若直接把**成交日**当 ``on_date``，则同一费率变动区间内的
#:   所有日期会变成成千上万个不同的缓存键 → ``_FEE_CACHE`` 全部 miss → 每笔都真查库，
#:   逐笔回测必然把连接池当查询接口（与本轮修掉的连接泄漏同源的性能问题）。
#:   费率是**低频变更**的参照数据（dim_trading_cost 全表最早 effective_from=2026-03-11，
#:   至今仅数次调整），故把日期**归约到其所属窗口的起始日**，同一窗口共用一个键。
_RATE_WINDOWS: dict = {}


def rate_window_starts(variety_code: str, kind: str = "FUTURE") -> list:
    """该品种所有费率变动区间的**起始日**（升序、去重），带进程内缓存。

    数据源 ``dim_trading_cost.effective_from`` 的 distinct 值 —— 即该品种费率
    发生变更的所有时点。``effective_to`` 与 ``effective_from`` 边界对齐，故只取
    起始日即可划分区间。
    """
    key = (str(variety_code).upper(), kind)
    got = _RATE_WINDOWS.get(key)
    if got is not None:
        return got
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT effective_from FROM dim_trading_cost "
                "WHERE upper(variety_code) = :v AND instrument_kind = :k "
                "AND effective_from IS NOT NULL ORDER BY 1"
            ),
            {"v": key[0], "k": kind},
        ).fetchall()
    starts = sorted({r[0] for r in rows if r[0] is not None})
    _RATE_WINDOWS[key] = starts
    return starts


# ---------------------------------------------------------------------------
# 主力合约反查（2026-10-07）
# ---------------------------------------------------------------------------
#: 为什么需要：交易所对**特定合约**给不同费率（螺纹钢 1/5/10 月主力 1‱、
#: 其余 0.2‱；碳酸锂 2601~2702 为 3.2‱ 而其余 0.8‱）。取费率时若不传合约码，
#: ``fee_per_lot`` 只能命中 ALL 档 → **主力合约成本被低估 5 倍**（RB 实测）。
#: 而回测读的是 888 主力连续线（无真实合约码），故需按交易日从
#: ``main_contract_map``（trade_date → underlying）反查当时的主力合约。
#:
#: 缓存：``(品种, 交易日) -> 合约码``。变动频率低（换月才变），逐笔回测全程
#: 只会命中「换月次数」级别的条目数。
_MAIN_CONTRACT: dict = {}


def main_contract_at(variety_code: str, d: date) -> str | None:
    """返回 ``d`` 当日该品种的主力合约码（如 ``RB2701``）；无数据返回 None。

    取 ``main_contract_map`` 中 ``trade_date <= d`` 的最近一行（与
    ``scripts/build_roll_segments.py:contract_at`` 同一口径：``COALESCE(underlying,
    main_symbol)``，即优先用 underlying，因为它才是真实可交易合约）。
    """
    key = (str(variety_code).upper(), d)
    if key in _MAIN_CONTRACT:
        return _MAIN_CONTRACT[key]
    with get_engine().connect() as conn:
        r = conn.execute(
            text(
                "SELECT COALESCE(underlying, main_symbol) FROM main_contract_map "
                "WHERE upper(product) = :v AND trade_date <= :d "
                "ORDER BY trade_date DESC LIMIT 1"
            ),
            {"v": key[0], "d": d},
        ).fetchone()
    out = r[0] if r else None
    _MAIN_CONTRACT[key] = out
    return out


def resolve_rate_date(variety_code: str, d: date, kind: str = "FUTURE") -> date:
    """把日期 ``d`` 归约到其所属费率变动区间的**起始日**（无窗口则原样返回）。

    - ``d`` 落在某区间内 → 返回该区间起始日（与 ``d`` 的费率**完全相同**）。
    - ``d`` 早于最早区间 → 返回**当期**区间起始日，等价于 :func:`fee_per_lot` 的
      fallback 语义（``effective_to IS NULL`` 的当期行），不改变成本数值。
    """
    starts = rate_window_starts(variety_code, kind)
    if not starts:
        return d
    prior = [s for s in starts if s <= d]
    if prior:
        return prior[-1]
    return starts[-1]


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
    """返回 ``(数字码, 交割月)``；无法解析时返回 ``(None, None)``。

    ⚠ 2026-10-03 修正两处：
    1) 原来要求数字码 >= 4 位，导致**郑交所 3 位合约码**（AP610 / CF701 / MA705）
       永远解析不出交割月 → CONTRACTS/MONTHS 档位对郑交所**全部失效**
       （实测 AP 有 CONTRACTS 行却永不命中）。
    2) 返回值改为「完整数字码」而非 ``[-4:]``，供档位匹配按位数自适应比较。
    """
    if not contract:
        return None, None
    from app.core.symbol_code import to_std
    s = to_std(contract)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 3:
        return None, None
    return digits, int(digits[-2:])


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
    cdigits = c4

    # 缓存命中则直接返回（见 _FEE_CACHE 处的说明：为什么安全、什么时候必须清）
    ck = (vc, action, cdigits, cmonth, d, kind)
    hit = _FEE_CACHE.get(ck)
    if hit is not None:
        _FEE_STATS["hit"] += 1
        return hit
    _FEE_STATS["miss"] += 1

    # ★两条查询必须在**同一个 session 作用域内**（2026-10-06 修连接泄漏）
    #   原实现在 `with session_scope()` 块**外**继续执行 fallback 查询
    #   （`s.execute(...)`），而 `session_scope` 的 finally 已 `session.close()`。
    #   SQLAlchemy 2.x 下 close() 后再 execute 会**重新开事务、从池里再取一条
    #   连接**，且该 s 此后永不 close → **每次 fee_per_lot 调用净泄漏一条连接**。
    #   费率表最早 effective_from=2026-03-11，历史回测**必然**走 fallback 分支，
    #   而 cost_coefficients 单次调用会调 fee_per_lot 3~6 次 → 批量回测
    #   必然耗尽连接池（pool_size=10 + max_overflow=20）。
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT scope_kind, scope_months, scope_contracts, fee_type, fee_value, "
            "       exchange_fee_value, broker_markup_type, broker_markup_value, "
            "       slip_ticks, effective_from, source, note "
            "FROM dim_trading_cost "
            "WHERE upper(variety_code) = :vc AND instrument_kind = :kind AND action = :act "
            "  AND effective_from <= :d AND (effective_to IS NULL OR effective_to > :d)"),
            {"vc": vc, "kind": kind, "act": action, "d": d}).fetchall()

        fell_back = False
        if not rows:
            _FEE_STATS["fallback"] += 1
            # ── 用户 2026-10-03 裁定：「没有历史费率就按当前费率计算」────────────
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

    best = None
    for r in rows:
        sk = r[0]
        if sk in ("CONTRACTS", "MONTHS"):
            # ── 2026-10-03 修正：交易所的档位常写成「月份 + 少数显式合约码」的组合
            #    （例：螺纹钢「1、5、10合约 & 2602、2603、2604」= months[1,2,3,4,5,10]
            #      + contracts['RB2602','RB2603','RB2604']）。原实现对 CONTRACTS 只看
            #    scope_contracts、完全无视 scope_months，导致 **主力合约(1/5/10 月)全部
            #    落空回落到 ALL 档**（实测 RB2610 取 0.2‱ 而非 1‱，成本低估 5 倍）。
            #    现改为：月份命中 或 显式合约码命中，任一即可。
            months = [int(x) for x in (r[1] or [])]
            hit = cmonth is not None and cmonth in months
            if not hit and cdigits:
                for code in (r[2] or []):
                    cd = "".join(ch for ch in str(code).upper() if ch.isdigit())
                    if not cd:
                        continue
                    # 兼容 3 位与 4 位混写：任一后缀对齐即视为同一合约
                    if cd == cdigits or cd[-3:] == cdigits[-3:] or cdigits.endswith(cd):
                        hit = True
                        break
            if not hit:
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
    out = TradingCost(
        variety_code=vc, action=action, fee_type=r[3], fee_value=float(r[4] or 0),
        exchange_fee_yuan=float(ex_src or 0),
        broker_markup_type=r[6] or "NONE",
        broker_markup_yuan=(float(r[7] or 0) if (r[6] or "NONE") in ("FIXED", "PCT") else 0.0),
        slip_ticks=float(r[8] or 0), scope_kind=r[0], effective_from=r[9],
        source=r[10], note=r[11],
        fee_as_of=d, fell_back_to_current=fell_back)
    if len(_FEE_CACHE) >= _FEE_CACHE_MAX:
        # 简单的容量上限：费率键空间有限（品种×动作×日期），正常不会触顶；
        # 触顶时整体清空重来，宁可多查也不让内存无界增长。
        _FEE_CACHE.clear()
        _FEE_STATS["evict"] += 1
    _FEE_CACHE[ck] = out
    return out


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

    :param on_date: **费率生效日**。回测历史**必须**显式传入，否则会用今天的费率
        算历史（滚动费率表的意义就在这里）。传入的日期会先经
        :func:`resolve_rate_date` **归约到所属费率变动区间的起始日** ——
        数值完全等价，但让同一区间内的所有日期共享同一个缓存键，
        逐笔回测才不会把连接池当查询接口。
    """
    from app.data.barstore import variety_spec
    sp = variety_spec(symbol)
    tick, mult = sp.get("tick_size"), sp["multiplier"]

    # ★ 2026-10-07：把日期归约到费率变动区间起点（数值等价、缓存友好）
    if on_date is not None:
        on_date = resolve_rate_date(_variety(symbol), on_date, kind)

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
