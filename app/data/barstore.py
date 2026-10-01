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

#: 各物理表实际持仓量列名（information_schema 实测 2026-10-01）。
#:   bar_5m/15m/30m/60m、minute_bar          → open_interest
#:   fut_kline/hourly_bar/daily_bar/contract_daily → oi
#:   minute_bar_adj                           → 两者皆无
#: 旧代码写死 `SELECT ... oi FROM bar_5m` 会对分钟表报错并降级成 oi=0，
#: 静默丢弃真实持仓量；SELECT 必须按物理列名取（见 load()）。
_OI_COL = {
    "bar_5m": "open_interest",
    "bar_15m": "open_interest",
    "bar_30m": "open_interest",
    "bar_60m": "open_interest",
    "minute_bar": "open_interest",
    "minute_bar_adj": None,   # 无持仓列 → oi 恒 0
    "fut_kline": "oi",
    "hourly_bar": "oi",
    "daily_bar": "oi",
    "contract_daily": "oi",
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


def variety_of(symbol: str) -> str:
    """给定任意符号（品种/主连/合约），返回其品种代码（CU/AU/FG）。

    走 dim_symbol → dim_contract 两路解析，全部失败则原样返回 symbol 的产品前缀。
    上层只需拿品种代码，所有 888/8888/KQ.m@ 转换由此屏蔽。
    """
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT product FROM dim_symbol WHERE symbol = :s"),
            {"s": symbol}).first()
        if row:
            return row[0]
        row = conn.execute(
            text("SELECT variety_code FROM dim_contract WHERE contract_code = :s"),
            {"s": symbol}).first()
        if row:
            return row[0]
    # 兜底：直接取字母前缀（AP2701 → AP）
    import re
    m = re.match(r"^([A-Za-z]+)", symbol)
    return m.group(1).upper() if m else symbol


def resolve_contract(contract_code: str) -> dict:
    """按标准合约码（AP2701）查合约配置表，返回生命周期/主力归属。

    返回字段：contract_code, variety_code, exchange, delivery_year, delivery_month,
             list_date, last_trade_date, main_symbol, is_main, is_active。
    dim_contract 未迁移时返回空 dict（调用方自行降级到 dim_symbol）。
    """
    try:
        with _engine().connect() as conn:
            row = conn.execute(text(
                "SELECT contract_code, variety_code, exchange, delivery_year, delivery_month, "
                "       list_date, last_trade_date, main_symbol, is_main, is_active "
                "FROM dim_contract WHERE contract_code = :c"
            ), {"c": contract_code}).first()
        if row:
            cols = ("contract_code", "variety_code", "exchange", "delivery_year",
                    "delivery_month", "list_date", "last_trade_date", "main_symbol",
                    "is_main", "is_active")
            return dict(zip(cols, row))
    except Exception:  # noqa: BLE001  dim_contract 未迁移 → 降级
        pass
    return {}


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
        caliber: continuous（未复权） / back_adj（**默认**，等差后复权）
                 / cont_adj（前复权，已废弃） / contract
        start, end: 时间下界/上界（含）
        limit: 只取最近 N 根（按时间倒序后取尾部）
        resolve: 是否把 symbol 解析为首选符号（False = 严格按给定符号取）

    Returns:
        DataFrame，列为 ts/open/high/low/close/volume/oi，按 ts 升序；
        caliber=back_adj 时**额外带一列 `adj_offset`**（该 bar 的复权偏移），
        执行层下单前用 `real = adj - adj_offset` 反解真实合约价。
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

    # 按物理表选对持仓列名（information_schema 实测；见 _OI_COL 注释）。
    # 写死 `oi` 会让分钟表 SELECT 报错并降级成 oi=0，静默丢弃真实持仓。
    oi_col = _OI_COL.get(route.table)   # 未知表 → None，直接走无 oi 分支
    if oi_col:
        sql = (f"SELECT {route.time_col} AS ts, open, high, low, close, "
               f"volume, {oi_col} AS oi FROM {route.table} "
               f"WHERE {' AND '.join(where)} ORDER BY {route.time_col} {order}")
        if limit:
            sql += f" LIMIT {int(limit)}"
        # 防御：若该列实际不存在（schema 漂移），退化为不带 oi（下游补 0）
        sql_no_oi = sql.replace(f", {oi_col} AS oi FROM", ", volume FROM")
    else:
        sql = (f"SELECT {route.time_col} AS ts, open, high, low, close, "
               f"volume FROM {route.table} "
               f"WHERE {' AND '.join(where)} ORDER BY {route.time_col} {order}")
        if limit:
            sql += f" LIMIT {int(limit)}"
        sql_no_oi = sql

    df, last_err, level = None, None, 0
    for attempt, s in enumerate((sql, sql_no_oi)):
        try:
            # **每次都开独立连接**：PG 里一条语句报错会把整个事务置为 aborted，
            # 在同一连接上重试只会得到 InFailedSqlTransaction（实测踩过，
            # 旧写法 `with connect(): try/except 再 pd.read_sql` 必然二次失败）
            with _engine().connect() as conn:
                df = pd.read_sql(text(s), conn, params=params)
            level = attempt
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if df is None:
        raise last_err
    if level:
        logger.warning(f"[barstore] {route.table} 无 oi 列，已降级取数（oi 置 0）")
    if "oi" not in df.columns:
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
    out = df[list(BASE_COLS)].sort_values("ts").reset_index(drop=True)

    # —— 复权：套 roll_segment 的累积偏移 ——
    # **必须在这里显式触发**：route.adj 是新增字段，若上层拿到 Route 自己拼 SQL
    # 而此处不改，就会出现「口径写着 back_adj、取到的其实是未复权」的静默退化。
    if route.adj:
        out = _apply_adj(out, route.adj, sym, freq)
    return out


def _apply_adj(df: pd.DataFrame, adj: str, symbol: str, freq: str) -> pd.DataFrame:
    """按 route.adj 对已取到的未复权序列做复权。

    目前只支持 adj='back'（等差后复权）。返回列 = BASE_COLS + ['adj_offset']，
    `adj_offset` 供执行层反解真实合约价（`real = adj - adj_offset`）。
    """
    if adj != "back":
        raise ValueError(f"未知复权方式 {adj!r}（只支持 'back'）")
    from app.data.back_adjust import apply_back_adjust  # 局部导入避免循环依赖

    if df.empty:
        return df.assign(adj_offset=pd.Series(dtype=float))
    f = {"5m": "min5", "15m": "min15", "30m": "min30", "60m": "min60"}.get(freq, freq)
    with _engine().connect() as conn:
        adj_df = apply_back_adjust(df, symbol, f, session=conn, time_col="ts")
    # adj_offset 追加在最末：老代码用 df[list(BASE_COLS)] 或按名取列不受影响
    return adj_df[list(BASE_COLS) + ["adj_offset"]].reset_index(drop=True)


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
