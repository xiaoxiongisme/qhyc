# -*- coding: utf-8 -*-
"""全合约分钟采集的交易时段门控（2026-11-06 采集效率优化）。

背景
----
``cron_fetch_1min`` 每 15 分钟跑一次，而 ``akshare.futures_zh_minute_sina``
固定返回最近约 1024 根（跨多个交易日），``--data-length`` 参数不生效。
收盘/休市期间每一轮都在重复拉取同一批数据：实测 2026-11-06 15:15 收盘后
至 20:00 共 18 轮，每轮 668 合约 / 68 万行 / 约 6 分钟，**实际新增 0 行**。

本模块提供纯函数 :func:`is_collection_window`，让采集入口在非交易时段直接返回，
省掉整轮 668 次 HTTP 请求。

设计原则
--------
* **覆盖全部日盘/夜盘**，并在收盘后留 20 分钟补尾（最后一根 bar 常在收盘后
  1~15 分钟才出现 / 才完整）。
* **宁可多采、不可漏采**：即使误判跳过，akshare 返回的历史窗口会在下一轮
  自动补齐，不会丢数据；``--force`` 可随时强制全量补采。
* **不依赖交易日历**：实测 ``l3_ref.trade_calendar`` 数据稀疏（只到 2026-10-08）
  且 ``sessions`` 为空，``l3_ref.cfg_holiday`` 是空表，均不足以做门控。
  法定节假日只会「多采几轮」，无正确性风险。
* 时段规则集中在本模块，调整只需改下面 4 个常量。

中国期货时段（Asia/Shanghai）
-----------------------------
* 日盘 09:00-10:15 / 10:30-11:30 / 13:30-15:00（金融期货略有差异）；
* 夜盘 21:00 起，最晚品种（贵金属/原油）到次日 02:30；
* 周五夜盘延续到**周六**凌晨；周日全天休市，周一凌晨无夜盘。
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

_SH_TZ = ZoneInfo("Asia/Shanghai")

#: 日盘窗口（含 10:15-10:30 小节休市与 11:30-13:30 午休；休市期采集无害）
DAY_START = time(8, 50)
DAY_END = time(15, 20)

#: 夜盘窗口（跨午夜）；20:50 提前 10 分钟，02:40 留 10 分钟补尾
NIGHT_START = time(20, 50)
NIGHT_END = time(2, 40)


def is_collection_window(now: datetime | None = None) -> bool:
    """当前是否处于「可能产出新分钟 bar」的窗口。

    ``now`` 必须是 aware datetime（或省略取当前上海时间）；naive 值按上海时间处理。
    """
    if now is None:
        now = datetime.now(_SH_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_SH_TZ)
    now = now.astimezone(_SH_TZ)
    t = now.time()
    wd = now.weekday()  # 0=Mon ... 6=Sun

    # 周日：全天休市（周一 09:00 才开盘，周日晚无夜盘）
    if wd == 6:
        return False

    # 周六：仅凌晨 00:00-02:40 属于周五夜盘延续
    if wd == 5:
        return time(0, 0) <= t <= NIGHT_END

    # 周一~周五：日盘
    if DAY_START <= t <= DAY_END:
        return True
    # 周一~周五：夜盘上半段（当日 20:50-23:59）
    if t >= NIGHT_START:
        return True
    # 周二~周五凌晨 00:00-02:40：前一日夜盘延续；周一凌晨无夜盘
    if wd != 0 and t <= NIGHT_END:
        return True
    return False


def describe(now: datetime | None = None) -> str:
    """给日志用的一句话说明（含时段与星期）。"""
    if now is None:
        now = datetime.now(_SH_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_SH_TZ)
    n = now.astimezone(_SH_TZ)
    return f"{n:%F %T %a} window={is_collection_window(n)}"


def is_hourly_collection_allowed(now: datetime | None = None) -> bool:
    """B5 (2026-11-07): gate for the akshare ``hourly_bar`` direct-pull chain.

    Combines two independent checks:

    1. :func:`is_collection_window` -- session window; this already handles the
       Friday-night carry into Saturday 00:00-02:40 and the Sunday shutdown.
    2. During the **day session only**, the authoritative futures calendar via
       :func:`app.data.trade_calendar.is_futures_trading_day` (G4 source =
       ``futures_rule`` + ``cfg_calendar_seed_meta.seeded_through``).

    Night-session bars are deliberately NOT calendar-gated: a Fri 21:00 or
    Sat 01:00 bar belongs to the *next* trading day, so its calendar date is
    not its trade date.  Gating those by ``date()`` would silently drop all
    night bars (a correctness regression far worse than a ghost bar).

    ``strict=False`` is intentional: dates beyond ``seeded_through`` are
    "unknown", and the calendar module logs a warning then falls back to
    weekday.  ``seed_futures_rule.py`` advances the boundary daily.
    """
    if not is_collection_window(now):
        return False
    n = now or datetime.now(_SH_TZ)
    if n.tzinfo is None:
        n = n.replace(tzinfo=_SH_TZ)
    n = n.astimezone(_SH_TZ)
    if DAY_START <= n.time() <= DAY_END:
        from app.data.trade_calendar import is_futures_trading_day
        return bool(is_futures_trading_day(n.date(), strict=False))
    return True
