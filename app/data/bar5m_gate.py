# -*- coding: utf-8 -*-
"""5M-P0A · bar_5m 逐品种「新鲜度 + 完备性」门（CL-20261121-04）。

为什么必须有这道门
------------------
生产实测 2026-11-21：``CU888`` / ``SC888`` 的 ``bar_5m`` 停在 11-07，而
``minute_bar`` 到 11-21；同时 ``synthesize_bars_incremental`` 的追平校验只比较
**整批符号的聚合 max**——只要有一个品种把 max 顶到最新，其余品种停更两周仍然
PASS。5m 策略若直接 ``barstore.load(..., freq='5m')``，就会拿两周前的 K 线出信号。

本模块提供与 V5X ``_complete_max`` 同口径的判定：

* **完备性**：5m 桶 ``B``（终点标签）已收完 ⟺ ``minute_bar`` 存在 ``ts == B``。
  因此 ``expected = floor_5m(max_minute_ts)``；``bar_max > expected`` 意味着
  读到了**在途半截桶**，同样拒绝（不是为了"更及时"，而是半截桶会生成幽灵信号）。
* **新鲜度**：``now - max_minute_ts <= max_age_min``，防止拿隔夜/停更数据出信号。
* **逐品种**：不设集合级 max，一个品种落后就拦一个。

默认只提供判定函数，不改变任何现有调用方行为（opt-in）。策略扫描器在取数后
显式调用 :func:`require_fresh` / :func:`filter_fresh`；守卫每日独立复查同一口径。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

FIVE_MIN = timedelta(minutes=5)
DEFAULT_MAX_AGE_MIN = 90
_LOOKBACK_DAYS = 3


class BarFreshnessError(RuntimeError):
    """逐品种新鲜度/完备性门未通过（fail-loud，禁止出信号）。"""


@dataclass(frozen=True)
class SymbolFreshness:
    symbol: str
    minute_max: datetime | None
    bar_max: datetime | None
    expected: datetime | None
    lag_minutes: float | None
    age_minutes: float | None
    status: str
    ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "minute_max": self.minute_max,
            "bar_max": self.bar_max,
            "expected": self.expected,
            "lag_minutes": self.lag_minutes,
            "age_minutes": self.age_minutes,
            "status": self.status,
            "ok": self.ok,
        }


def _norm_ts(ts: datetime | None, like: datetime | None) -> datetime | None:
    """统一 aware/naive 身份，避免直接比较抛 TypeError（V5X 同款坑）。"""
    if ts is None or like is None:
        return ts
    if like.tzinfo is None and ts.tzinfo is not None:
        return ts.replace(tzinfo=None)
    if like.tzinfo is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=like.tzinfo)
    return ts


def floor_5m(ts: datetime) -> datetime:
    """向下对齐到 5 分钟网格终点标签（分钟数是 5 的倍数）。"""
    minute = (ts.minute // 5) * 5
    return ts.replace(minute=minute, second=0, microsecond=0)


def complete_bucket(max_minute_ts: datetime | None) -> datetime | None:
    """由该品种最新分钟标签推出「最后一根已收完的 5m 桶」。

    ``ts`` 是终点标签：``max(ts)=14:07`` ⇒ 14:05 桶已收完（含 14:05 分钟），
    14:07 所在的 14:10 桶仍在途。故直接向下对齐到 5 分钟即可。
    """
    if max_minute_ts is None:
        return None
    return floor_5m(max_minute_ts)


def evaluate(
    symbols: Iterable[str],
    minute_max: Mapping[str, datetime | None],
    bar_max: Mapping[str, datetime | None],
    now: datetime | None = None,
    max_age_min: int | None = DEFAULT_MAX_AGE_MIN,
) -> list[SymbolFreshness]:
    """逐品种判定，返回每个符号一条 :class:`SymbolFreshness`（顺序同入参）。

    不通过的状态：

    ``NO_MINUTE`` 该品种近窗内无分钟数据（连基准都没有）；
    ``NO_BAR`` 有分钟但 bar_5m 近窗内无该品种（CU/SC 式整段停更）；
    ``LAGGING`` bar_5m 落后于最后一根已收完桶；
    ``AHEAD_UNSAFE`` bar_5m 出现了比已收完桶更晚的标签（在途半截桶）；
    ``STALE`` 分钟数据本身超过 ``max_age_min``（停更/非交易时段）；
    ``OK`` 唯一放行状态。
    """
    results: list[SymbolFreshness] = []
    anchor = now
    for sym in symbols:
        m = _norm_ts(minute_max.get(sym), anchor)
        b = _norm_ts(bar_max.get(sym), anchor)
        exp = complete_bucket(m)
        anchor_for_sym = anchor if anchor is not None else m
        age = None
        if m is not None and anchor_for_sym is not None:
            age = (anchor_for_sym - m).total_seconds() / 60.0
        lag = None
        if exp is not None and b is not None:
            lag = (exp - b).total_seconds() / 60.0

        if m is None:
            status = "NO_MINUTE"
        elif age is not None and max_age_min is not None and age > max_age_min:
            status = "STALE"
        elif b is None:
            status = "NO_BAR"
        elif lag is not None and lag > 0:
            status = "LAGGING"
        elif lag is not None and lag < 0:
            status = "AHEAD_UNSAFE"
        else:
            status = "OK"
        results.append(SymbolFreshness(
            symbol=str(sym), minute_max=m, bar_max=b, expected=exp,
            lag_minutes=lag, age_minutes=age, status=status, ok=(status == "OK"),
        ))
    return results


def filter_fresh(
    symbols: Iterable[str],
    minute_max: Mapping[str, datetime | None],
    bar_max: Mapping[str, datetime | None],
    now: datetime | None = None,
    max_age_min: int | None = DEFAULT_MAX_AGE_MIN,
) -> tuple[list[str], list[SymbolFreshness]]:
    """返回 ``(允许交易的品种, 全部判定明细)``。"""
    rows = evaluate(symbols, minute_max, bar_max, now, max_age_min)
    return [r.symbol for r in rows if r.ok], rows


def require_fresh(
    symbols: Iterable[str],
    minute_max: Mapping[str, datetime | None],
    bar_max: Mapping[str, datetime | None],
    now: datetime | None = None,
    max_age_min: int | None = DEFAULT_MAX_AGE_MIN,
) -> list[SymbolFreshness]:
    """任一品种不过门即抛 :class:`BarFreshnessError`（fail-loud）。"""
    rows = evaluate(symbols, minute_max, bar_max, now, max_age_min)
    bad = [r for r in rows if not r.ok]
    if bad:
        detail = ", ".join(
            f"{r.symbol}:{r.status}(bar={r.bar_max},expected={r.expected})" for r in bad)
        raise BarFreshnessError(f"[5M-P0A] bar_5m 逐品种门未通过：{detail}")
    return rows


def load_minute_max(session, symbols: list[str],
                    lookback_days: int = _LOOKBACK_DAYS) -> dict[str, datetime]:
    """从 ``public.minute_bar``（continuous 口径）逐品种取最新分钟标签。"""
    if not symbols:
        return {}
    rows = session.execute(
        _MINUTE_SQL, {"syms": list(symbols), "days": lookback_days}).all()
    return {str(s): _norm_ts(ts, datetime.now()) for s, ts in rows if ts is not None}


def load_bar5m_max(session, symbols: list[str],
                   lookback_days: int = _LOOKBACK_DAYS) -> dict[str, datetime]:
    """从 ``l1_mkt.bar_5m`` 逐品种取最新桶标签。"""
    if not symbols:
        return {}
    rows = session.execute(
        _BAR_SQL, {"syms": list(symbols), "days": lookback_days}).all()
    return {str(s): _norm_ts(ts, datetime.now()) for s, ts in rows if ts is not None}


_MINUTE_SQL = """
SELECT symbol, max(ts) FROM public.minute_bar
WHERE symbol = ANY(:syms) AND ts >= now() - make_interval(days => :days)
GROUP BY symbol
"""
_BAR_SQL = """
SELECT symbol, max(bucket) FROM l1_mkt.bar_5m
WHERE symbol = ANY(:syms) AND bucket >= now() - make_interval(days => :days)
GROUP BY symbol
"""
