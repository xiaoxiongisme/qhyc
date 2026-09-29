# -*- coding: utf-8 -*-
"""BarStore —— 数据层 L2 统一取数接口（六层解耦的核心闸门）。

为什么要有这一层
----------------
2026-09-28 全库盘点发现：上层（调度 / 回测 / 因子 / 策略）各自裸写 SQL 取行情，
同一个"15 分钟主连"在不同脚本里有四种取法，且 daily/hourly 的 cont_adj 用
`KQ.m@EXCHANGE.PROD`、min15/30/60 用 `XXX888`，**跨周期 join 会静默少数据**。

BarStore 用一个入口收敛所有取数：
    load(symbol, freq, caliber, start, end) -> DataFrame
并强制：
  1. 周期 × 口径必须已在 `caliber.ROUTES` 显式登记（未登记即报错，不许临时拼 SQL）；
  2. symbol 一律经 `dim_symbol` 解析（品种 → 首选命名空间符号）；
  3. 返回列统一：`ts, open, high, low, close, volume, oi`（缺失列补 0）。

用法
----
    from app.data import barstore
    df = barstore.load("FG", "15m", "continuous", "2024-01-01", "2024-12-31")
    df = barstore.load("FG888", "hourly", "continuous")   # 亦可传完整符号

注意
----
本模块**只取数，不做任何指标计算**；指标/信号属于策略层与因子层。
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import text

from app.core.db import get_engine
from app.data.caliber import DEFAULT_CALIBER, get_route

logger = logging.getLogger(__name__)

#: 统一返回列
BASE_COLS = ("ts", "open", "high", "low", "close", "volume", "oi")

#: 各表实际列名 → 统一列名（volume/open_interest 命名不一致的历史包袱）
_COL_ALIAS = {
    "volume": "volume",
    "vol": "volume",
    "open_interest": "oi",
    "oi": "oi",
}


class SymbolNotFoundError(LookupError):
    """symbol 在 dim_symbol 中不存在（或无法解析到唯一首选符号）。"""


def _engine():
    return get_engine()


# ---------------------------------------------------------------------------
# symbol 解析
# ---------------------------------------------------------------------------

def resolve_symbol(symbol: str, namespace: str = "main",
                   strict: bool = False) -> str:
    """把「品种」或「任意命名空间符号」解析为首选符号。

    - 传入 `FG`   → 查 dim_symbol 取 namespace 的首选（默认 main，即 FG888）
    - 传入 `FG888` → 原样返回（已登记）
    - 传入 `KQ.m@CZCE.FG` → 若该品种有 main 首选且 strict=False，返回 FG888；
      strict=True 时原样返回（用于明确要读天勤原生序列的场景）

    Raises:
        SymbolNotFoundError: 无法解析
    """
    if not symbol:
        raise SymbolNotFoundError("symbol 为空")
    with _engine().connect() as conn:
        # 1) 直接命中已登记符号
        row = conn.execute(
            text("SELECT symbol, product, namespace FROM dim_symbol WHERE symbol = :s"),
            {"s": symbol}).first()
        if row:
            if strict:
                return row[0]
            # 非 strict：若该品种有首选且当前不是首选，升级为首选
            pref = conn.execute(
                text("SELECT symbol FROM dim_symbol "
                     "WHERE product = :p AND namespace = :ns AND is_preferred"),
                {"p": row[1], "ns": namespace}).first()
            return pref[0] if pref else row[0]
        # 2) 当作品种代码
        pref = conn.execute(
            text("SELECT symbol FROM dim_symbol "
                 "WHERE upper(product) = upper(:p) AND namespace = :ns AND is_preferred"),
            {"p": symbol, "ns": namespace}).first()
        if pref:
            return pref[0]
        anyrow = conn.execute(
            text("SELECT symbol FROM dim_symbol WHERE upper(product) = upper(:p) LIMIT 1"),
            {"p": symbol}).first()
        if anyrow:
            return anyrow[0]
    raise SymbolNotFoundError(
        f"dim_symbol 中找不到 {symbol!r}（namespace={namespace}）。"
        f"请先跑 migrations/001_data_layer_foundation.sql 回填维度表")


def namespace_conflicts() -> list[dict]:
    """列出存在命名空间冲突的品种（同时有 888 与 KQ.m@ 两套码）。

    跨周期 join 前必须显式处理这些品种，否则会静默少数据。
    """
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT product, main_symbol, tqsdk_symbol, symbol_count "
            "FROM v_symbol_canonical WHERE has_namespace_conflict ORDER BY product"
        )).fetchall()
    return [dict(zip(("product", "main_symbol", "tqsdk_symbol", "symbol_count"), r))
            for r in rows]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def load(symbol: str, freq: str = "hourly",
         caliber: str = DEFAULT_CALIBER,
         start: Optional[str] = None, end: Optional[str] = None,
         limit: Optional[int] = None,
         resolve: bool = True) -> pd.DataFrame:
    """统一取数入口。

    Args:
        symbol: 品种（如 FG）或完整符号（如 FG888 / KQ.m@CZCE.FG）
        freq:   1m/5m/15m/30m/60m/hourly/daily
        caliber: continuous（默认，回测基准） / cont_adj（统计校验） / contract
        start, end: 时间下界/上界（含）
        limit: 只取最近 N 根（按时间倒序后取尾部）
        resolve: 是否把 symbol 解析为首选符号（False = 严格按给定符号取）

    Returns:
        DataFrame，列为 ts/open/high/low/close/volume/oi，按 ts 升序。
        无数据时返回**空 DataFrame**（不抛异常）。
    """
    route = get_route(freq, caliber)
    sym = resolve_symbol(symbol) if resolve else symbol

    where = [f"{route.table}.symbol = :sym"] if not route.where else \
            [f"symbol = :sym", route.where]
    params: dict = {"sym": sym}
    if start:
        where.append(f"{route.time_col} >= :start")
        params["start"] = start
    if end:
        where.append(f"{route.time_col} <= :end")
        params["end"] = end

    order = "DESC" if limit else "ASC"
    sql = (f"SELECT {route.time_col} AS ts, open, high, low, close, "
           f"volume, oi FROM {route.table} "
           f"WHERE {' AND '.join(where)} ORDER BY {route.time_col} {order}")
    if limit:
        sql += f" LIMIT {int(limit)}"

    with _engine().connect() as conn:
        try:
            df = pd.read_sql(text(sql), conn, params=params)
        except Exception as e:  # noqa: BLE001
            # 列名差异兜底：部分表无 oi 列
            logger.warning(f"[barstore] {route.table} 取数降级（{e}），尝试无 oi 列")
            sql2 = (f"SELECT {route.time_col} AS ts, open, high, low, close, "
                    f"volume FROM {route.table} "
                    f"WHERE {' AND '.join(where)} ORDER BY {route.time_col} {order}")
            if limit:
                sql2 += f" LIMIT {int(limit)}"
            df = pd.read_sql(text(sql2), conn, params=params)
            df["oi"] = 0

    if df.empty:
        return pd.DataFrame(columns=list(BASE_COLS))

    if limit:
        df = df.iloc[::-1].reset_index(drop=True)   # 恢复升序

    df.columns = [str(c).lower() for c in df.columns]
    df = df.rename(columns=_COL_ALIAS)
    for c in BASE_COLS:
        if c not in df.columns:
            df[c] = 0
    df["ts"] = pd.to_datetime(df["ts"])
    return df[list(BASE_COLS)].sort_values("ts").reset_index(drop=True)


def load_many(symbols: list[str], freq: str = "hourly",
              caliber: str = DEFAULT_CALIBER,
              start: Optional[str] = None, end: Optional[str] = None
              ) -> dict[str, pd.DataFrame]:
    """批量取数（逐个委托 load，失败不中断，返回成功的部分）。"""
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            out[s] = load(s, freq, caliber, start, end)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[barstore] {s} 取数失败：{e}")
    return out


def coverage(symbol: Optional[str] = None) -> pd.DataFrame:
    """数据覆盖度体检：各 (freq, caliber) 的符号数与时间范围。

    用于替代"人工猜哪张表有数据"，回测前必查（对应方法论教训第 6 条）。
    """
    rows = []
    for (freq, cal), r in sorted(__import__("app.data.caliber", fromlist=["ROUTES"]).ROUTES.items()):
        w = [f"symbol = :sym"] if not symbol else [f"symbol = :sym"]
        params = {"sym": symbol} if symbol else {}
        sql = (f"SELECT count(*), count(DISTINCT symbol), "
               f"min({r.time_col}), max({r.time_col}) FROM {r.table}")
        conds = ([r.where] if r.where else []) + (["symbol = :sym"] if symbol else [])
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        try:
            with _engine().connect() as conn:
                n, nsym, t0, t1 = conn.execute(text(sql), params).first()
            rows.append(dict(freq=freq, caliber=cal, table=r.table,
                             rows=n, symbols=nsym, start=t0, end=t1))
        except Exception as e:  # noqa: BLE001
            rows.append(dict(freq=freq, caliber=cal, table=r.table,
                             rows=None, symbols=None, start=None, end=str(e)[:60]))
    return pd.DataFrame(rows)
