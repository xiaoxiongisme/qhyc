# -*- coding: utf-8 -*-
"""数据质量标记（DQ flag）：登记可疑数据行，**不修改原数据**。

背景（2026-10-08 全面测试）
--------------------------
项6 数据审计发现真实脏数据，且**不适合直接改数**：

    l0_raw.daily_bar : 88 行 OHLC 不自洽（close 超出 [low, high] 区间）
    l0_raw.minute_bar: 18 个品种日出现 non_positive_price
    l1_mkt/bar_*     : 各 10~11 行（同一批 low=0 的上游污染）

定性结论：**89% 的 daily_bar 脏数据集中在 2015-2017（IS 样本内区间）**，
来源是**上游交易所历史日线**（非本仓采集 bug）。这批数据本身是"上游发布的事实"，
擅自改写会破坏可追溯性（无法再回溯原始值、也无法证明改对了）。故本模块的定位是：

    **登记 + 提供排除依据**，把"用不用脏数据"的决定权交给调用方（回测/因子）。

为什么不修而只标
----------------
1. **可追溯性**：原值一旦改写就不可逆；上游若某天修正数据，我们无从发现"曾改过"。
2. **口径风险**：把 low=0 改成 low=open 是**猜测**（真实 low 可能是 1265 也可能 1260）。
   猜错会让 ATR / 吊灯止损 / 回撤算错，且**错得看不出来**。
3. **占比极低**：daily_bar 88/169,593 = 0.05%；但集中在 IS 段，影响的是"结论可信度"
   而非"系统能否运行"——这正适合用"标记 + 排除"而非"改数"。

调用方怎么用
------------
回测/因子计算在取数后过滤::

    from app.data.quality import flagged_keys, drop_flagged

    # 回测：排除脏日
    bars = drop_flagged(bars, "daily_bar", date_col="trade_date")
    # 因子：按 (symbol, trade_date) 排除
    bad = flagged_keys("daily_bar")            # -> {(symbol, date), ...}
    vals = [v for v in vals if (v.symbol, v.trade_date) not in bad]

⚠ 与既有因子的取舍一致性
------------------------
本模块**不自动**改变任何生产计算结果——那会让"上线前后的数字悄悄变化"。
它先提供事实，再由各计算路径显式选择是否排除。这样每个数字的变化都能回答
"因为我们排除了 88 个脏日"（可解释），而不是"数字变了不知道为什么"。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 标记表名 → (schema, 表, 时间列)
_FLAG_TABLES: dict[str, tuple[str, str, str]] = {
    "daily_bar": ("l0_raw", "daily_bar", "trade_date"),
    "hourly_bar": ("l0_raw", "hourly_bar", "trade_datetime"),
    "minute_bar": ("l0_raw", "minute_bar", "ts"),
    "bar_5m": ("l1_mkt", "bar_5m", "bucket"),
    "bar_15m": ("l1_mkt", "bar_15m", "bucket"),
    "bar_30m": ("l1_mkt", "bar_30m", "bucket"),
    "bar_60m": ("l1_mkt", "bar_60m", "bucket"),
}

#: OHLC 逻辑一致性判据（SQL 侧与 Python 侧必须一致，改一处要改两处）。
#: high 应 >= max(open, close)；low 应 <= min(open, close)；价格须为正。
BAD_OHLC_SQL = (
    "(high < greatest(open,close) or low > least(open,close) "
    "or open<=0 or high<=0 or low<=0 or close<=0)"
)


def is_bad_row(o: Any, h: Any, l: Any, c: Any) -> bool:
    """单行 OHLC 是否可疑（与 :data:`BAD_OHLC_SQL` 同口径，供写入前拦截）。"""
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return True                      # 解析不出来 = 可疑，交给上层决策
    if not all(v == v for v in (o, h, l, c)):   # NaN
        return True
    if min(o, h, l, c) <= 0:
        return True
    return bool(h < max(o, c) or l > min(o, c))


def flagged_keys(table_name: str, symbols: Iterable[str] | None = None) -> set[tuple[str, Any]]:
    """返回该表被标记的 ``(symbol, date)`` 集合。

    Parameters
    ----------
    table_name:
        ``daily_bar`` / ``hourly_bar`` / ``minute_bar`` / ``bar_5m`` 等（见 :data:`_FLAG_TABLES`）。
    symbols:
        可选，只取指定品种（避免全表扫）。

    Returns
    -------
    set[tuple[str, Any]]
        ``(symbol, date)`` 元组集合；表不存在或查询失败时返回**空集合**
        （⚠ 空集合 = "没有脏数据"这一断言，调用方若要fail-loud 须自行校验表存在性，
        见 :func:`quality_table_exists`）。
    """
    import datetime as _dt

    from sqlalchemy import text

    from app.core.db import get_engine

    if table_name not in _FLAG_TABLES:
        raise KeyError(f"未知表 {table_name}；可用：{sorted(_FLAG_TABLES)}")
    with get_engine().connect() as conn:
        sql = ("SELECT symbol, trade_date FROM l0_raw.data_quality_flag "
               "WHERE table_name = :t")
        params: dict = {"t": table_name}
        if symbols:
            syms = list(symbols)
            sql += " AND symbol = ANY(:s)"
            params["s"] = syms
        rows = conn.execute(text(sql), params).fetchall()
    return {(r[0], r[1] if isinstance(r[1], _dt.date) else str(r[1])[:10]) for r in rows}


def drop_flagged(rows, table_name: str, *, symbol_col: str = "symbol",
                 date_col: str = "trade_date"):
    """从 DataFrame 中剔除被标记的行（**返回新对象，不改入参**）。

    用于回测取数后过滤。未知列/无脏数据时原样返回（不复制、不报错）。
    """
    import pandas as pd  # 局部依赖

    if rows is None or len(rows) == 0:
        return rows
    if not hasattr(rows, "columns"):        # 非 DataFrame（如 list）→ 不处理
        return rows
    if symbol_col not in rows.columns or date_col not in rows.columns:
        return rows
    bad = flagged_keys(table_name)
    if not bad:
        return rows
    syms = set(rows[symbol_col].astype(str))
    days = set(rows[date_col].astype(str).str[:10])
    # 先按表内实际出现的 (symbol, date) 组合过滤，避免全表拉脏键
    keys = set()
    for s in syms:
        for d in days:
            if (s, d) in bad:
                keys.add((s, d))
    if not keys:
        return rows
    mask = rows.apply(
        lambda r: (str(r[symbol_col]), str(r[date_col])[:10]) not in keys, axis=1)
    out = rows[mask]
    logger.warning("[quality] %s 剔除脏数据 %d/%d 行（标记表 %d 条命中）",
                   table_name, len(rows) - len(out), len(rows), len(bad))
    return out.reset_index(drop=True)


def quality_summary() -> dict[str, dict[str, int]]:
    """返回各表×原因的标记计数（诊断/巡检用；失败时返回空 dict）。"""
    from sqlalchemy import text

    from app.core.db import get_engine

    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(
                "SELECT table_name, reason, count(*) FROM l0_raw.data_quality_flag "
                "GROUP BY 1,2")).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("[quality] 标记表不可用: %s", e)
        return {}
    out: dict[str, dict[str, int]] = {}
    for t, reason, n in rows:
        out.setdefault(t, {})[reason] = n
    return out


def quality_table_exists() -> bool:
    """标记表是否可用（供调用方做 fail-loud 判断，避免把"表缺失"误当"无脏数据"）。"""
    from sqlalchemy import text

    from app.core.db import get_engine

    try:
        with get_engine().connect() as conn:
            return bool(conn.execute(text(
                "SELECT to_regclass('l0_raw.data_quality_flag')")).scalar())
    except Exception:  # noqa: BLE001
        return False


__all__ = ["BAD_OHLC_SQL", "is_bad_row", "flagged_keys", "drop_flagged",
           "quality_summary", "quality_table_exists"]