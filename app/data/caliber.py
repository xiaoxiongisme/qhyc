# -*- coding: utf-8 -*-
"""口径注册表（六层解耦 · 数据层 L2 的"字典"）。

为什么需要它
------------
历史代码中，同一个"15 分钟主连"在不同脚本里有不同取法：
  * 有人查 `fut_kline(freq='min15', kind='continuous')`
  * 有人查 `bar_15m`（888 主连，未复权）
  * 复权序列又在 `fut_kline(kind='cont_adj')`
  * daily/hourly 的 cont_adj 用 `KQ.m@EXCHANGE.PROD`，而 min15/30/60 用 `XXX888`
于是出现「跨周期 join 静默少数据」——不报错，只是结果不对（PRD 差距分析已点名）。

本模块把「周期 × 口径 → 具体表」的映射固化成**唯一权威**，上层只能通过
`BarStore` 取数，禁止裸 SQL。

口径定义（PRD 铁律，不得混淆）
------------------------------
  continuous  未复权主连（888 法）：**回测与对账的基准**。换月有跳空，
              但它是真实可交易序列的价格轨迹。
  cont_adj    加法平移前复权主连：**已废弃**（会产生负价，I888 最低 −1058.5，51.4% bar 为负），
              已从 CALIBERS / ROUTES 移除，仅保留历史交叉校验数据，不可经 BarStore 取数。
  contract    标准合约序列（品种大写 + YYMM，如 AP2701）：逐合约，不连续。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import logger

#: 合法口径
#:   回测基准自 2026-09-30 起改为 **back_adj（等差后复权）**（用户拍板）；
#:   cont_adj（加法前复权）因会产生负价（I888 最低 −1058.5，51.4% bar 为负）被废弃。
CALIBERS = ("continuous", "back_adj", "contract")  # cont_adj 已废弃（产生负价），C7 移除

#: 周期枚举（与 fut_kline.freq / bar_* 对齐）
FREQS = ("1m", "5m", "15m", "30m", "60m", "hourly", "daily")

#: 回测与对账的默认口径
#: 2026-09-30 用户拍板：**改用等差后复权**。
#: 依据（84 品种实测）：前复权 13 个品种历史价转负、且每次换月需全历史重写；
#:   等差后复权把负价降到 3 个品种、ATR 零失真、且**只追加不必重算**。
#: 注意：已有策略建议显式声明 caliber，避免依赖默认值。
DEFAULT_CALIBER = "back_adj"


@dataclass(frozen=True)
class Route:
    """一条取数路由：周期+口径 → 表 + 过滤条件。"""

    table: str
    #: 额外 SQL 条件（已参数化占位符用 :name 形式，由 BarStore 绑定）
    where: str = ""
    #: 时间列名
    time_col: str = "bucket"
    #: 是否需要在 symbol 上做 888/8888 等后缀约束
    symbol_transform: str = ""
    note: str = ""
    #: 复权方式："" = 不复权；"back" = **等差后复权**（套 roll_segment.cum_offset）
    #: 由 app.data.back_adjust.apply_back_adjust 执行
    adj: str = ""


#: 路由表：(freq, caliber) -> Route
#: 依据（2026-09-28 全库实测）：
#:   fut_kline       freq ∈ {daily,hourly,min15,min30,min60} × kind ∈ {continuous,cont_adj,contract}
#:   bar_5m/15m/30m/60m  未复权主连区间棒（symbol=XXX888，另有 XXX8888 指数连）
#:   minute_bar / minute_bar_adj  1 分钟（原始 / 已复权）
#:   daily_bar / hourly_bar       日线 / 小时线（含多 src）
#:   contract_daily               逐合约日线
ROUTES: dict[tuple[str, str], Route] = {
    # —— 1 分钟 ——
    ("1m", "continuous"): Route("minute_bar", time_col="ts",
                                note="1 分钟原始主连（未复权）"),
    # ("1m", "cont_adj"):   Route("minute_bar_adj", time_col="ts",
    #                             note="已废弃：1 分钟前复权主连（产生负价，不可达）"),
    # —— 5 分钟 ——
    ("5m", "continuous"): Route("bar_5m", time_col="bucket",
                                note="5 分钟未复权主连（888）"),
    ("5m", "back_adj"):   Route("bar_5m", time_col="bucket", adj="back",
                                note="5 分钟等差后复权（bar_5m + roll_segment）"),
    # —— 15 / 30 / 60 分钟 ——
    ("15m", "continuous"): Route("bar_15m", time_col="bucket", note="15m 未复权主连"),
    ("30m", "continuous"): Route("bar_30m", time_col="bucket", note="30m 未复权主连"),
    ("60m", "continuous"): Route("bar_60m", time_col="bucket", note="60m 未复权主连"),
    ("15m", "back_adj"):   Route("bar_15m", time_col="bucket", adj="back",
                                note="15m 等差后复权（bar_15m + roll_segment）"),
    ("30m", "back_adj"):   Route("bar_30m", time_col="bucket", adj="back",
                                note="30m 等差后复权"),
    ("60m", "back_adj"):   Route("bar_60m", time_col="bucket", adj="back",
                                note="60m 等差后复权"),
    # 已废弃：加法前复权（会产生负价，见 scripts/deprecate_cont_adj.py）—— 已从 CALIBERS 移除。
    # 下列路由保留为文档参考（get_route 在 caliber not in CALIBERS 时已先抛 CaliberError，不可达）。
    # ("15m", "cont_adj"):   Route("fut_kline", where="freq='min15' AND kind='cont_adj'",
    #                              time_col="trade_datetime",
    #                              note="DEPRECATED 前复权：会产生负价且须全量重算"),
    # ("30m", "cont_adj"):   Route("fut_kline", where="freq='min30' AND kind='cont_adj'",
    #                              time_col="trade_datetime", note="DEPRECATED 前复权"),
    # ("60m", "cont_adj"):   Route("fut_kline", where="freq='min60' AND kind='cont_adj'",
    #                              time_col="trade_datetime", note="DEPRECATED 前复权"),
    # —— 小时线：融合策略主周期，必须单一源（akshare），杜绝双源混读 ——
    # 2026-10-04 (G9) 退役 fut_kline.continuous：hourly 未复权主连改读 hourly_bar（L0 原始主连）
    ("hourly", "continuous"): Route("hourly_bar", where="src='akshare'",
                                    time_col="trade_datetime",
                                    note="小时线未复权主连（G9 后改读 hourly_bar，fut_kline.continuous 已退役）"),
    # ("hourly", "cont_adj"):   Route("fut_kline", where="freq='hourly' AND kind='cont_adj'",
    #                                 time_col="trade_datetime",
    #                                 note="已废弃：小时线前复权（不可达）"),
    # —— 日线 ——
    # 2026-10-04 (G9) 退役 fut_kline.continuous：daily 未复权主连改读 daily_bar（L0 原始主连）
    ("daily", "continuous"): Route("daily_bar", where="symbol LIKE '%888'",
                                   time_col="trade_date",
                                   note="日线未复权主连（G9 后改读 daily_bar，fut_kline.continuous 已退役）"),
    # ("daily", "cont_adj"):   Route("fut_kline", where="freq='daily' AND kind='cont_adj'",
    #                                time_col="trade_datetime",
    #                                note="已废弃：日线前复权（不可达）"),
    ("daily", "contract"):   Route("contract_daily", time_col="trade_date",
                                   note="逐合约日线（symbol=品种大写+YYMM）"),
    # 【C8 决策·方案A】日线/小时线维持 continuous（未复权）基准（daily_bar / hourly_bar）。
    # 后复权仅对已有 roll_segment 段的分钟频(5/15/30/60m)生效；日线/小时线无段，
    # default_caliber_for 对它们显式回退 continuous 并告警（绝不静默用 raw）。此为唯一口径决策。
}


class CaliberError(ValueError):
    """口径/周期组合不合法或尚未登记。"""


def default_caliber_for(freq: str, *, warn: bool = True) -> str:
    """**按周期渐进**的默认口径（评审补强 **G3 / P0**）。

    背景：PRD §B9 曾定默认口径为 ``cont_adj``，但 §B2 承认 ``roll_segment``
    **只有 min5/15/30/60 段、缺日线/小时线段** → 默认口径一生效，日线/小时线
    就因无段而 fail-loud，等于「默认口径对自身最重要的日线源先坏」。

    处置：默认口径**只对已构建 offset 段的周期**生效；未覆盖的周期显式回退到
    ``continuous`` 并**告警**（**绝不静默返回 raw** —— 静默回落是本项目最主要的
    缺陷类型）。段补齐后（B2 完成）自动生效，无需改代码。
    """
    if freq in FREQS and (freq, DEFAULT_CALIBER) in ROUTES:
        return DEFAULT_CALIBER
    if warn:
        logger.warning(
            f"[caliber] freq={freq} 尚无 {DEFAULT_CALIBER} 段（roll_segment 未覆盖该周期），"
            f"**显式回退 continuous**（未复权）。回测若依赖复权价须先为该周期建段。")
    return "continuous"


def get_route(freq: str, caliber: str) -> Route:
    """取路由，未登记则抛出 CaliberError（强制显式登记，避免走偏到裸 SQL）。"""
    if caliber not in CALIBERS:
        raise CaliberError(f"未知口径 {caliber!r}，合法值 {CALIBERS}")
    if freq not in FREQS:
        raise CaliberError(f"未知周期 {freq!r}，合法值 {FREQS}")
    r = ROUTES.get((freq, caliber))
    if r is None:
        raise CaliberError(
            f"未登记的取数组合 freq={freq} caliber={caliber}。"
            f"请在 app/data/caliber.py ROUTES 显式登记后再取数（禁止绕过 BarStore 裸 SQL）")
    return r


def register_route(freq: str, caliber: str, route: Route) -> None:
    """运行时扩展路由（新增数据源/新周期时使用）。"""
    ROUTES[(freq, caliber)] = route
