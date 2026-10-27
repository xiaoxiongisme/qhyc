# -*- coding: utf-8 -*-
"""保证金模型（纯函数，无 DB / 无 pandas 依赖，可单测）。

为什么单独成模块
----------------
2026-10-26 夜盘事故的复盘结论是：**保证金被低估的根因不是"重仓"，而是三条口径错误**。

1. **锁仓按双边收**：郑商所对同一合约的双向持仓**不实行单向大边**，多空各收一份。
   （上期所 / 大商所 / 中金所 / 广期所实行单向大边，只收较大边。）
   当事账户 MA701 多 4 手 + 空 4 手，净敞口 0，但保证金照收 8 手。
2. **保证金率是百分数**：``futures_rule.margin_ratio`` 存的是 ``8.0000`` 而非 ``0.08``，
   直接相乘会把需求放大 100 倍（或与 0.08 混用后低估 100 倍）。本模块统一归一化。
3. **临时提保是"率"与"价"同时变**：涨停后交易所按**涨停价**重算名义，且大概率同步提保，
   两个因子相乘才是真实压力（8%→15% 约 1.9 倍，涨停约 1.06 倍，合计约 2 倍）。

因此本模块只做四件事，全部是确定性算术：
``normalize_rate`` / ``leg_margin`` / ``scenario_usage`` / ``lock_policy``。
所有阈值、缓冲倍数都由调用方传入，**不在模块内藏默认值**（除交易所先天规则）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# 交易所保证金收取制度（先天规则，不是参数）
# ---------------------------------------------------------------------------
#: 实行「单向大边」的交易所：同一客户同一合约双向持仓，只按较大边收取保证金。
#: 郑商所（CZCE）**不实行**，锁仓收双边——这正是 10-26 事故的放大器。
SINGLE_SIDE_EXCHANGES: frozenset[str] = frozenset(
    {"SHFE", "INE", "DCE", "CFFEX", "GFEX"}
)

#: 交易所代码归一（容忍中文与大小写）
_EX_ALIAS = {
    "上期所": "SHFE", "上海期货交易所": "SHFE", "SHFE": "SHFE",
    "能源中心": "INE", "上海国际能源交易中心": "INE", "INE": "INE",
    "大商所": "DCE", "大连商品交易所": "DCE", "DCE": "DCE",
    "郑商所": "CZCE", "郑州商品交易所": "CZCE", "CZCE": "CZCE",
    "中金所": "CFFEX", "中国金融期货交易所": "CFFEX", "CFFEX": "CFFEX",
    "广期所": "GFEX", "广州期货交易所": "GFEX", "GFEX": "GFEX",
}


def norm_exchange(exchange: str | None) -> str:
    """交易所代码归一；无法识别时返回 ``""``（调用方须按 fail-loud 处理）。"""
    if not exchange:
        return ""
    s = str(exchange).strip()
    return _EX_ALIAS.get(s, _EX_ALIAS.get(s.upper(), ""))


def lock_policy(exchange: str | None) -> str:
    """返回该交易所的锁仓保证金制度：``"SINGLE_SIDE"`` 或 ``"BOTH_SIDES"``。

    无法识别的交易所返回 ``"BOTH_SIDES"``——**保守取值**：多收不会误判安全，
    少收会直接导致盘中追保。
    """
    return "SINGLE_SIDE" if norm_exchange(exchange) in SINGLE_SIDE_EXCHANGES else "BOTH_SIDES"


# ---------------------------------------------------------------------------
# 保证金率归一化
# ---------------------------------------------------------------------------
def normalize_rate(raw: float | None, *, fallback: float | None = None) -> float | None:
    """把保证金率归一为小数（0.08）。

    ``futures_rule.margin_ratio`` 的口径是**百分数**（akshare ``futures_rule`` 返回
    "8.0000" 表示 8%）。本函数对两种口径都容忍：

    * ``raw > 1.5``  → 视为百分数，除以 100（8.0 → 0.08；15.0 → 0.15）
    * ``0 < raw <= 1.5`` → 视为已是小数，原样返回

    阈值取 1.5 而非 1.0 的理由：历史上出现过 120% 的异常值（临近交割、风险合约），
    1.2 这种值若按小数解释会得到 120% 保证金率（荒谬但不致命），按百分数解释得 1.2%
    （更荒谬）。两者都不可信，故 >1.5 一律按百分数并在调用方告警。

    返回 ``None`` 表示无可用值（调用方必须走 fallback 或判 UNKNOWN，禁止静默当 0）。
    """
    if raw is None:
        return fallback
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return fallback
    if v <= 0:
        return fallback
    if v > 1.5:
        v = v / 100.0
    return v


# ---------------------------------------------------------------------------
# 持仓腿与规则
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionLeg:
    """一条持仓腿（同一合约的多头与空头是**同一条腿的两个字段**）。

    ``long_lots`` / ``short_lots`` 必须分开填，禁止只给净持仓——锁仓时净持仓为 0
    但保证金不为 0，这是本模块存在的全部理由。
    """

    real_symbol: str
    exchange: str
    long_lots: int = 0
    short_lots: int = 0
    price: float = 0.0          # 参考价（优先昨结算，其次最新价）
    multiplier: float = 0.0

    @property
    def gross_lots(self) -> int:
        return self.long_lots + self.short_lots

    @property
    def net_lots(self) -> int:
        return self.long_lots - self.short_lots

    @property
    def is_locked(self) -> bool:
        """双向持仓（锁仓）——保证金按制度可能收双边，必须单独告警。"""
        return self.long_lots > 0 and self.short_lots > 0


@dataclass(frozen=True)
class RuleRow:
    """来自 ``futures_rule`` 的当日规则。"""

    margin_ratio: float | None = None   # 百分数或小数，交给 normalize_rate
    price_limit: float | None = None    # 百分数或小数，同上
    multiplier: float | None = None
    special_note: str | None = None
    adjust_note: str | None = None


@dataclass
class UsageResult:
    """一次保证金测算的完整结果（每一项都要能回答"钱花在哪了"）。"""

    equity: float
    margin_required: float = 0.0
    usage: float = 0.0
    gross_notional: float = 0.0
    net_notional: float = 0.0
    by_symbol: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)      # 缺规则的合约
    notes: list[str] = field(default_factory=list)


def leg_margin(
    leg: PositionLeg,
    rule: RuleRow | None,
    *,
    price: float | None = None,
    rate: float | None = None,
    rate_buffer: float = 1.0,
) -> tuple[float, str | None]:
    """单合约保证金需求，返回 ``(金额, 缺失原因)``。

    ``price`` 缺省用 ``leg.price``；``rate`` 缺省用 ``rule.margin_ratio`` 归一化后
    再乘 ``rate_buffer``（再提保倍数）。
    """
    px = float(price if price is not None else leg.price)
    if px <= 0:
        return 0.0, f"{leg.real_symbol}: 参考价缺失或非正({px})"
    mult = float((rule.multiplier if rule and rule.multiplier else leg.multiplier) or 0)
    if mult <= 0:
        return 0.0, f"{leg.real_symbol}: 合约乘数缺失"
    if leg.gross_lots <= 0:
        return 0.0, None

    r = normalize_rate(rate if rate is not None else (rule.margin_ratio if rule else None))
    if r is None:
        return 0.0, f"{leg.real_symbol}: 保证金率缺失（futures_rule 无当期记录）"
    r = r * rate_buffer

    # 制度决定收单边还是双边
    if lock_policy(leg.exchange) == "SINGLE_SIDE" and leg.is_locked:
        lots_charged = max(leg.long_lots, leg.short_lots)
    else:
        lots_charged = leg.gross_lots

    return lots_charged * mult * px * r, None


def scenario_usage(
    legs: Iterable[PositionLeg],
    rules: dict[str, RuleRow],
    equity: float,
    *,
    rate_buffer: float = 1.0,
    limit_shock: float = 0.0,
    price_override: dict[str, float] | None = None,
) -> UsageResult:
    """测算一组持仓的保证金占用。

    参数
    ----
    equity : 动态权益（分母）。``<=0`` 直接抛错——用 0 当分母会得到 inf 而被当"安全"。
    rate_buffer : 再提保倍数（1.0=不提保；1.5=按当前率再上浮 50%）。
    limit_shock : 价格冲击（0=按现价；1.0=恰好一个停板；可 >1 表示连续板）。
    price_override : 逐合约强制覆盖价格（如用涨停价）。
    """
    if equity <= 0:
        raise ValueError(f"[fail-loud] equity 必须为正，收到 {equity!r}（禁止用 0 做分母）")

    res = UsageResult(equity=float(equity))
    for leg in legs:
        rule = rules.get(leg.real_symbol)
        px = None
        if price_override and leg.real_symbol in price_override:
            px = price_override[leg.real_symbol]
        elif limit_shock:
            lim = normalize_rate(rule.price_limit if rule else None)
            if lim is None:
                res.missing.append(f"{leg.real_symbol}: 涨跌停幅度缺失，无法做极端情景")
                px = leg.price
            else:
                # 空头被涨停打击、多头被跌停打击——压力方向取对保证金不利的一侧
                direction = 1.0 if leg.short_lots >= leg.long_lots else -1.0
                px = leg.price * (1.0 + direction * lim * limit_shock)

        amt, why = leg_margin(leg, rule, price=px, rate_buffer=rate_buffer)
        if why:
            res.missing.append(why)
        res.margin_required += amt
        res.by_symbol[leg.real_symbol] = amt
        mult = float((rule.multiplier if rule and rule.multiplier else leg.multiplier) or 0)
        p = float(px if px is not None else leg.price)
        res.gross_notional += leg.gross_lots * mult * p
        res.net_notional += abs(leg.net_lots) * mult * p

        if leg.is_locked:
            pol = lock_policy(leg.exchange)
            res.notes.append(
                f"{leg.real_symbol}: 多{leg.long_lots}空{leg.short_lots} 锁仓，"
                f"按 {pol} 计收（{norm_exchange(leg.exchange) or '未知交易所'}）"
            )

    res.usage = res.margin_required / res.equity
    return res


# ---------------------------------------------------------------------------
# 涨跌停距离
# ---------------------------------------------------------------------------
def limit_band(settle: float, rule: RuleRow | None) -> tuple[float, float] | None:
    """由昨结算与板幅算 ``(跌停价, 涨停价)``；板幅缺失返回 ``None``。"""
    lim = normalize_rate(rule.price_limit if rule else None)
    if lim is None or settle <= 0:
        return None
    return settle * (1.0 - lim), settle * (1.0 + lim)


def limit_room(price: float, settle: float, rule: RuleRow | None) -> dict[str, float] | None:
    """当前价距涨跌停的剩余空间。

    返回 ``{"to_up_pct", "to_down_pct", "to_up_atr_mult"}`` 之类的最小集合：
    ``to_up_pct`` = 距涨停还差百分之几（0.02 = 还剩 2% 空间）。
    """
    band = limit_band(settle, rule)
    if band is None or price <= 0:
        return None
    down, up = band
    span = up - down
    if span <= 0:
        return None
    return {
        "to_up_pct": max(0.0, (up - price) / price),
        "to_down_pct": max(0.0, (price - down) / price),
        "pos_in_band": max(0.0, min(1.0, (price - down) / span)),
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
VERDICT_RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2, "UNKNOWN": 3}


def worst(*verdicts: str) -> str:
    """取最保守的判定（UNKNOWN 视为最保守：不知道就不许开新仓）。"""
    if not verdicts:
        return "GREEN"
    return max(verdicts, key=lambda v: VERDICT_RANK.get(v, 3))


def verdict_from_usage(
    usage_now: float,
    usage_extreme: float,
    *,
    warn_now: float = 0.40,
    stop_now: float = 0.55,
    stop_extreme: float = 0.60,
) -> tuple[str, list[str]]:
    """按占用率给三色灯。返回 ``(verdict, reasons)``。

    阈值的经济学含义（不是拍的）：
    * ``warn_now=0.40``：留 60% 缓冲，对应「保证金率被提到 1.9 倍」时仍不追保。
    * ``stop_now=0.55``：已无缓冲，任何提保都直接击穿。
    * ``stop_extreme=0.60``：极端情景（一个板 + 提保 1.5 倍）下的硬上限。
    """
    reasons: list[str] = []
    v = "GREEN"
    if usage_extreme >= stop_extreme:
        v = worst(v, "RED")
        reasons.append(
            f"极端情景占用 {usage_extreme:.0%} ≥ {stop_extreme:.0%}："
            f"一个停板 + 提保即可能追保"
        )
    if usage_now >= stop_now:
        v = worst(v, "RED")
        reasons.append(f"当前占用 {usage_now:.0%} ≥ {stop_now:.0%}：已无提保缓冲")
    elif usage_now >= warn_now:
        v = worst(v, "YELLOW")
        reasons.append(f"当前占用 {usage_now:.0%} ≥ {warn_now:.0%}：只允许减仓")
    return v, reasons
