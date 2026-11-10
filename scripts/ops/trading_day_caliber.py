"""交易日形态判据的单一真源（CL-20261110-07）。

用途
----
``scripts/ops/backfill_bars_from_5m.py`` 自动探测缺口日时，必须区分
「有前夜盘尾段」与「无前夜盘尾段」两种**都合法**的形态：

* 周二~周五自然日：含 00:00~01:00 的凌晨段（前一自然日夜盘的延续），约 93 根 5m；
* 周一自然日 / 节后首个交易日：前一自然日休市，无凌晨段，约 80 根 5m。

旧判据按 **星期几** 分组求众数，于是节后首日（实测 2026-10-08 周四，前一日
10-07 国庆休市，只有 80 根）被拿去和「周四 93 根」比，误判成缺口，进而把
新浪在凌晨吐出的合成脏 bar 当成真实数据补进 L1（AO/SS 事故，见 CL-20261109-20）。

真源选择（别用停更的那张表）
----------------------------
``l3_ref.trade_calendar`` 实测**停更到 2026-10-08**（2026-10 整月仅 1 行），
不能当判据；``public.futures_rule`` 由 ``seed_futures_rule`` 每日刷新，实测
覆盖到 2026-11-10，且已有 ``DATA-CALENDAR-FRESH`` /
``OPS-DATA-LAG-TRADE-DAY`` 两条不变量锁住它。故交易日集合一律取自
``futures_rule``。

本模块刻意不依赖 SQLAlchemy / ``app.*``，使 ``guard_invariants.py`` 可以零副作用
import 并对判据做正反哨兵验证（``BACKFILL-GAP-CALENDAR-WIRED``）。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

TRADING_DAY_TABLE = "public.futures_rule"
TRADING_DAY_SQL = f"""
    SELECT DISTINCT trade_date FROM {TRADING_DAY_TABLE}
    WHERE trade_date >= :lo AND trade_date <= :hi
"""


def has_prev_session(day: date, trading_days: set[date]) -> bool:
    """该自然日凌晨是否有「前夜盘尾段」。

    判据：前一**自然日**是交易日 ⇒ 其夜盘 21:00 开、尾段落在 ``day`` 的
    00:00~02:30。周一（前一自然日周日）与节后首日（前一自然日休市）均为 False。

    注意这里比较的是「自然日」而非「交易日」：夜盘属于下一交易日，但按
    ``bucket::date`` 落在**当天晚上**和**次日凌晨**两个自然日里，所以决定
    ``day`` 有没有凌晨段的，是 ``day - 1`` 晚上开没开盘。
    """
    return (day - timedelta(days=1)) in trading_days


def session_shape(day: date, trading_days: set[date]) -> tuple[bool, bool]:
    """返回 ``(有前夜盘尾段, 有当晚夜盘前段)``，作为缺口判据的分组键。

    只按「前夜盘有无」二分还不够：节前最后交易日（实测 2026-09-30 → 下一
    交易日 10-08，跨 8 天）当晚**没有夜盘**，根数又比完整日少一截，会被
    拿去和完整日比而误判。这里把「当晚有没有夜盘」也纳入分组：

    * ``(True, True)``   普通完整日（周二~周五）
    * ``(False, True)``  周一 / 节后首日（无凌晨段）
    * ``(True, False)``  节前最后交易日（无当晚夜盘）
    * ``(False, False)`` 长假两侧叠加（极罕见）

    判据：下一交易日与当日相隔 ≤ 3 天（周五→周一 = 3，夜盘照常）视为
    有当晚夜盘；跨 > 3 天视为长假、无夜盘。窗口末端查不到下一交易日时
    保守返回 False —— 它会落进单独一组，绝不会被拿去和完整日比。
    """
    following = [t for t in trading_days if t > day]
    has_next_night = bool(following) and (min(following) - day).days <= 3
    return has_prev_session(day, trading_days), has_next_night


def mode_of(values: list[int]) -> int:
    """与 PostgreSQL ``mode() WITHIN GROUP (ORDER BY n)`` 同语义。

    频次降序、并列取**值较小**者；空序列直接抛错（不许静默退化成 0）。
    """
    if not values:
        raise ValueError("mode_of: 空序列")
    return sorted(Counter(values).items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
