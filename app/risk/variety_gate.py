# -*- coding: utf-8 -*-
"""P2 · 品种减法（Variety Gate）。

为什么做减法，而不是继续加策略
------------------------------
归档里的三条硬结论（均为样本外证据，非样本内择优）：

1. **周期上移是唯一有 OOS 支撑的方向**：点位触达日线 IS PF 1.58 → OOS 0.90（衰减 −0.55，
   OOS PF>1 仅 42%）；换成 60m 后 OOS PF 中位 1.12、**PF>1 占 83%**。
2. **分散在小资金上不成立**：组合回测显示小组合不可行，系统级配置需要 177–199 万；
   4~5 万本金下"59 品种分散"只是把有限的保证金摊薄成一堆无法承受 1 手波动的碎仓。
3. **盈亏取决于选市场/方向，不取决于调参**：162 组参数扫描的 PF 中位恒在 0.92–0.93，
   换参命中率 0/4。

因此本模块只回答一个问题：**以我现在的权益，哪 2–3 个品种既能开得出 1 手、
又能把保证金占用压在安全线内、且在样本外被验证过？**

纪律
----
* 白名单只来自 **walk-forward 样本外**通过的品种，禁止把样本内最优当准入。
* 「1 手保证金占用率」是准入的第一道门——单品种超过权益的 12% 直接出局，
  因为一个跳空就能打出追保。
* 输出**必须两列**：固定选品集 与 自身重选品。只给一列的净利结论不可引用（归档教训 #17）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.risk import margin_model as mm

# ---------------------------------------------------------------------------
# 样本外验证白名单（来源：qhyc level_touch walk-forward，2026-10-25）
# ---------------------------------------------------------------------------
#: 日线 OOS 三折叠全部 PF>1 的品种（严格集）
VALIDATED_DAILY: tuple[str, ...] = ("AG", "AU", "JM", "SC", "SF", "SR", "SS")
#: 日线亏损、60m 翻正的品种（周期误判而非策略失效）
VALIDATED_60M: tuple[str, ...] = ("RU", "SC")
#: 逐品种推荐周期（其余默认 daily）
BEST_FREQ: dict[str, str] = {"SC": "60m", "RU": "60m"}

#: 单品种 1 手保证金占权益上限（超过即出局：一个跳空就可能追保）
MAX_SINGLE_USAGE = 0.12
#: 全部持仓合计占用上限（与 preopen_gate 的 stop_now 对齐）
MAX_TOTAL_USAGE = 0.55


@dataclass
class VarietyCandidate:
    product: str
    freq: str
    margin_per_lot: float
    usage_per_lot: float
    multiplier: float
    ref_price: float
    validated: bool
    note: str

    def as_row(self) -> dict[str, Any]:
        return {
            "product": self.product, "freq": self.freq,
            "margin_per_lot": round(self.margin_per_lot),
            "usage_per_lot": round(self.usage_per_lot, 4),
            "multiplier": self.multiplier, "ref_price": round(self.ref_price, 2),
            "validated": self.validated, "note": self.note,
        }


def tradable_varieties(
    session: Session,
    *,
    account: str,
    trade_date: date,
    equity: float,
    max_single_usage: float = MAX_SINGLE_USAGE,
    max_total_usage: float = MAX_TOTAL_USAGE,
    include_unvalidated: bool = False,
) -> dict[str, Any]:
    """按「资金可行性 + 样本外验证」筛出可交易品种。

    返回 ``{"candidates": [...], "excluded": [...], "budget": ...}``。
    """
    if equity <= 0:
        raise ValueError("[fail-loud] equity 必须为正")

    rows = session.execute(text("""
        SELECT DISTINCT ON (code) code, margin_ratio, multiplier, price_limit
        FROM futures_rule
        WHERE trade_date <= :d AND code IS NOT NULL
        ORDER BY code, trade_date DESC
    """), {"d": trade_date}).all()
    if not rows:
        logger.warning("[variety_gate] futures_rule 无可用记录，返回空清单")
        return {"candidates": [], "excluded": [], "budget": equity * max_total_usage,
                "reason": "futures_rule 为空"}

    prices = _latest_prices(session, [r[0] for r in rows], trade_date)
    multip = {r[0]: float(r[2]) if r[2] else 0.0 for r in rows}
    rates = {r[0]: r[1] for r in rows}

    cands: list[VarietyCandidate] = []
    excluded: list[dict[str, Any]] = []
    for code in sorted(rates):
        validated = code in VALIDATED_DAILY or code in VALIDATED_60M
        if not validated and not include_unvalidated:
            excluded.append({"product": code, "why": "无样本外验证记录"})
            continue
        px = prices.get(code)
        mult = multip.get(code)
        rate = mm.normalize_rate(rates.get(code))
        if not px or not mult or not rate:
            excluded.append({"product": code, "why": "缺价格/乘数/保证金率"})
            continue
        margin = px * mult * rate * 1.03          # 含期货公司加收
        usage = margin / equity
        freq = BEST_FREQ.get(code, "daily")
        note = []
        if usage > max_single_usage:
            note.append(f"1 手即占 {usage:.1%} > 上限 {max_single_usage:.0%}，当前资金开不了")
        if code in VALIDATED_60M and code in VALIDATED_DAILY:
            note.append("日线与 60m 均通过")
        elif code in VALIDATED_60M:
            note.append("仅 60m 通过，勿用日线")
        cands.append(VarietyCandidate(
            product=code, freq=freq, margin_per_lot=margin, usage_per_lot=usage,
            multiplier=mult, ref_price=px, validated=validated,
            note="；".join(note) or "通过"))

    ok = [c for c in cands if c.usage_per_lot <= max_single_usage]
    ok.sort(key=lambda c: c.usage_per_lot)
    return {
        "candidates": [c.as_row() for c in ok],
        "excluded": excluded,
        "budget": round(equity * max_total_usage, 2),
        "max_symbols": (int(max_total_usage / max_single_usage)
                        if max_single_usage > 0 else 0),
        "note": (f"权益 {equity:,.0f} 元；单品种 1 手 ≤ {max_single_usage:.0%} 权益，"
                 f"合计 ≤ {max_total_usage:.0%}；建议同时持有不超过 "
                 f"{int(max_total_usage / max_single_usage)} 个品种"),
    }


def _latest_prices(session: Session, codes: Sequence[str], trade_date: date) -> dict[str, float]:
    """各品种取主力合约最近结算价（用于算 1 手名义）。

    ⚠ JOIN 必须用 ``main_contract_map.underlying``：``main_symbol`` 是主连符号
    （``AG888``），而 ``contract_daily`` 只存真实合约（``AG2612``），
    用错字段会让 JOIN 永远空 → 品种门静默返回空清单。实测踩过。
    """
    rows = session.execute(text("""
        SELECT m.product, c.settle
        FROM main_contract_map m
        JOIN contract_daily c
          ON c.symbol = COALESCE(m.underlying, m.main_symbol)
         AND c.trade_date = (SELECT MAX(trade_date) FROM contract_daily
                             WHERE symbol = COALESCE(m.underlying, m.main_symbol)
                               AND trade_date <= :d)
        WHERE m.trade_date = (SELECT MAX(trade_date) FROM main_contract_map
                              WHERE trade_date <= :d)
          AND m.product = ANY(:p)
    """), {"d": trade_date, "p": list(codes)}).all()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


# ---------------------------------------------------------------------------
# 接受标准：净利必须同时给「全量」与「剔 TOP3」两列
# ---------------------------------------------------------------------------
def _as_symbol_net(t: Any) -> tuple[str, float]:
    """把一条记录统一成 ``(symbol, net)``，接受二元组或 ``TradeRow`` 形态。

    对 ``TradeRow`` 取 ``real_symbol``（不是 ``symbol``——后者是品种码，
    会丢掉到期月，用于逐合约统计时会误合并）。缺失字段直接抛错，
    不做 ``getattr(..., None)`` 兜底：宁可炸，也不要静默算成 0。
    """
    if isinstance(t, (tuple, list)):
        if len(t) != 2:
            raise TypeError(f"trades 元素须为 (symbol, net) 二元组，收到长度 {len(t)}")
        return str(t[0]), float(t[1])
    sym = getattr(t, "real_symbol", None)
    net = getattr(t, "net", None)
    if sym is None or net is None:
        raise TypeError(
            f"trades 元素须为 (symbol, net) 二元组或带 real_symbol/net 的对象，"
            f"收到 {type(t).__name__}"
        )
    return str(sym), float(net)


def product_code(symbol: str) -> str:
    """``AG2612`` → ``AG``；``MA701`` → ``MA``；已品种码则原样返回（幂等）。

    为什么必须有这一步（2026-10-27 实测的静默失效）：
    ``trade_journal.real_symbol`` 存的是**真实合约**（``AG2612``），而
    ``VALIDATED_DAILY`` 这类白名单存的是**品种码**（``AG``）。直接比字符串
    永远不相等 → ``net_fixed`` 恒为 0 → 接受标准恒判 FAIL 且理由写着
    "固定选品集净利 0 ≤ 0"。**不报错、量级正常、看起来像策略真的不行**。
    """
    s = str(symbol).strip().upper()
    m = re.match(r"^([A-Z]+)", s)
    return m.group(1) if m else s


def acceptance_check(
    trades: Iterable[Any],
    *,
    fixed_symbols: Sequence[str] | None = None,
    min_trades: int = 30,
) -> dict[str, Any]:
    """把「净利」拆成可引用的四列。

    ``trades`` 支持两种形态（内部统一化为 ``(品种码, 净盈亏)``）：

    * ``(symbol, net)`` 二元组序列——``symbol`` 可以是真实合约 ``AG2612``；
    * 带 ``real_symbol`` / ``net`` 属性的对象序列（即
      :class:`~app.risk.trade_journal.TradeRow`）。

    符号一律经 :func:`product_code` 归一为**品种码**，否则 ``fixed_symbols``
    （白名单存的是品种码）永远匹配不上，``net_fixed`` 恒为 0——见该函数注释。

    返回：

    * ``net_all``：全量净利（**这一列单独存在时不可引用**）
    * ``net_ex_top3``：剔除贡献最大的 3 笔后的净利（抗尾部依赖）
    * ``net_fixed``：限定在固定选品集上的净利（抗"重选品"美化）
    * ``top5_share``：前 5 大盈利笔占全部盈利的比例（集中度）
    * ``verdict``：通过 / 不通过及理由

    归档教训 #17：同一策略同一区间，仅换选品集，OOS 净利从 109.7 万 → 6.9 万。
    单列净利 = 不可引用。
    """
    rows = [_as_symbol_net(t) for t in trades]
    rows = [(product_code(s), float(n)) for s, n in rows]
    if not rows:
        return {"verdict": "NO_DATA", "reason": "无交易记录"}
    nets = sorted((n for _, n in rows), reverse=True)
    net_all = sum(nets)
    net_ex_top3 = sum(nets[3:]) if len(nets) > 3 else 0.0
    wins = [n for n in nets if n > 0]
    top5 = sum(wins[:5]) if wins else 0.0
    total_win = sum(wins) or 1.0

    by_sym: dict[str, float] = {}
    for s, n in rows:
        by_sym[s] = by_sym.get(s, 0.0) + n
    net_fixed = (sum(by_sym.get(s, 0.0) for s in fixed_symbols)
                 if fixed_symbols else None)

    reasons: list[str] = []
    if len(rows) < min_trades:
        reasons.append(f"样本仅 {len(rows)} 笔 < {min_trades}，不足以下结论")
    if net_ex_top3 <= 0:
        reasons.append(f"剔除最大 3 笔后净利 {net_ex_top3:,.0f} ≤ 0：收益依赖尾部")
    if top5 / total_win > 0.5:
        reasons.append(f"前 5 笔占全部盈利 {top5 / total_win:.0%}：集中度过高")
    if net_fixed is not None and net_fixed <= 0:
        reasons.append(f"固定选品集净利 {net_fixed:,.0f} ≤ 0")

    return {
        "n": len(rows),
        "net_all": round(net_all, 2),
        "net_ex_top3": round(net_ex_top3, 2),
        "net_fixed": round(net_fixed, 2) if net_fixed is not None else None,
        "top5_share": round(top5 / total_win, 4),
        "by_symbol": {k: round(v, 2) for k, v in
                      sorted(by_sym.items(), key=lambda kv: kv[1])},
        "verdict": "PASS" if not reasons else "FAIL",
        "reason": "；".join(reasons),
    }


def check_trade_journal(
    session: Session, account: str, *, since: date | None = None,
    fixed_symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """从 ``trade_journal`` 直接跑接受标准（实盘/回放统一口径）。"""
    sql = ("SELECT real_symbol, net FROM trade_journal WHERE account = :a")
    params: dict[str, Any] = {"a": account}
    if since:
        sql += " AND close_at >= :s"
        params["s"] = since
    rows = session.execute(text(sql), params).all()
    return acceptance_check([(r[0], r[1]) for r in rows], fixed_symbols=fixed_symbols)
