# -*- coding: utf-8 -*-
"""融合策略 K 线统一取数入口（P0-3 修复 / 2026-11-07）。

设计铁律（用户口径 2026-11-07）
--------------------------------
回测与实盘必须读**同一条 K 线**。允许的唯一差别是「同一张表的原始维度」与
「同一张表的后复权维度」；**不允许两条独立采集链各读一张表**。

    唯一物理真源   l1_mkt.bar_60m      （L1，由 l0_raw.minute_bar 会话网格合成）
    后复权维度     l2_adj.bar_60m_ba   （同一批 bucket，price = raw + adj_offset）
    禁止作为真源   l0_raw.hourly_bar   （akshare 独立直拉链，与 bar_60m 不是同一条 K）

体检实测（`数据层全面体检报告_20261107.md`）：
  * `hourly_bar(src=akshare)` 与 `bar_60m` 同桶 close 差 ±2 点、volume 差最高 100+ 倍；
  * `bar_60m_ba` 与 `bar_60m` 在 118,646 行主连上恒等式
    `ba.close - l1.close = adj_offset` 的 maxdiff = 0.0000、bad = 0。
  * 注：8888 指数连本就不进 BA（`sp_build_l2_back_adjusted` 只处理 `^[A-Za-z]+888$`），
    旧报告把它算成「BA 覆盖缺口 90,342 行」是口径误判，已在本次修复中撤销。

实现要点
--------
1. **两端共用同一条 SQL**：`FROM l1_mkt.bar_60m l LEFT JOIN l2_adj.bar_60m_ba b
   ON (symbol, bucket)`。dt 集合与原始 OHLC 永远来自 L1，后复权只是在这批行上
   叠加 `adj_offset`；即使物化表停更，回测与实盘的桶也逐位一致。
2. `adj_offset` 来源优先级：物化列 → 未覆盖尾部用
   `app.data.back_adjust.apply_back_adjust(freq="min60", time_col="dt")` 基于
   `public.roll_segment` 现场计算 → 仍缺失则 fail-loud（`strict=True`），
   **绝不静默返回 0 偏移**（这正是 P0-2 的教训）。
3. 防御：`l2_adj.bar_60m_ba` 若出现重复 (symbol, bucket)，LEFT JOIN 会放大行数，
   直接抛错而不是让信号层读到重复 K。
4. `strict=True` 时校验该品种在 `roll_segment` 有 `min60` 段；P0-2 已实测段表
   `max(seg_start)=2026-10-30 15:00`，10-30 后 5 个 change_flag 未成段，
   后复权维度上线前必须先补段（见生产执行门禁清单）。

执行层契约
----------
后复权维度返回的 `adj_offset` 列必须随信号传到下单，
`app.execution.reverse_price.reverse_price(..., price_space="adj")` 会按
`real = adj_price - adj_offset` 反解成真实合约价。
默认 `fusion.kline_src = "bar_60m"`（原始维度）→ 价格即盘面真实价，无需反解；
切到 `"bar_60m_ba"` 时必须同步把执行层 `price_space` 设为 `"adj"`，否则会下错价。

调用方
------
* 实盘：`app/strategies/fusion_signal.py::_read_src(..., context="live")`
* 回测：`app/backtest/fusion_backtest.py::_read_hourly(..., context="backtest")`
* 验收：`verify_same_kline()` 给出恒等式与桶集合对账结果。
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from sqlalchemy import text

from app.core.logging import logger

RAW_TABLE = "l1_mkt.bar_60m"
BA_TABLE = "l2_adj.bar_60m_ba"
SEG_TABLE = "public.roll_segment"

#: roll_segment.freq 的取值。**不是** "hourly"（caliber 里 hourly 与 60m 同源，
#: 但段表只有 min5/min15/min30/min60）——BarStore 旧代码正是把 "hourly" 原样
#: 传进去，导致查不到任何段、后复权偏移静默退化为 0，见 P0-2b 补丁。
FREQ_SEG = "min60"

_BASE_COLS = ["dt", "open", "high", "low", "close"]
_PRICE_COLS = ["open", "high", "low", "close"]

RAW_SRCS = frozenset({"bar_60m", "continuous", "raw", "l1"})
ADJ_SRCS = frozenset({"bar_60m_ba", "back_adj", "adj", "ba"})
_CONTEXTS = frozenset({"live", "backtest", "audit"})


def normalize_src(src: str) -> str:
    """把历史遗留的各种 src 别名收敛成 raw / back_adj 两个**同表维度**。

    任何指向 `l0_raw.hourly_bar` 的独立采集源（akshare / csv / agg_1m）都直接
    拒绝，而不是"兼容着读"——兼容就等于允许回测与实盘分叉。
    """
    s = (src or "").strip().lower()
    if s in RAW_SRCS:
        return "raw"
    if s in ADJ_SRCS:
        return "back_adj"
    raise ValueError(
        f"[fusion_kline] src={src!r} 不是允许的 K 线维度；"
        f"只允许 {sorted(RAW_SRCS | ADJ_SRCS)}（唯一真源 {RAW_TABLE}）。"
        f"l0_raw.hourly_bar 的 akshare/csv/agg_1m 是独立采集链，已按 P0-3 禁用"
    )


def _fetch(session, symbol: str, limit: int | None,
           start, end) -> pd.DataFrame | None:
    """两端共用的唯一 SQL：L1 取桶 + LEFT JOIN 物化后复权偏移。"""
    sql = (
        f"SELECT l.bucket, l.open, l.high, l.low, l.close, b.adj_offset "
        f"FROM {RAW_TABLE} l "
        f"LEFT JOIN {BA_TABLE} b "
        f"  ON b.symbol = l.symbol AND b.bucket = l.bucket "
        f"WHERE l.symbol = :sym"
    )
    p: dict = {"sym": symbol}
    if start is not None:
        sql += " AND l.bucket >= :start"
        p["start"] = pd.Timestamp(start)
    if end is not None:
        sql += " AND l.bucket <= :end"
        p["end"] = pd.Timestamp(end)
    sql += " ORDER BY l.bucket " + ("DESC" if limit else "ASC")
    if limit:
        sql += " LIMIT :lim"
        p["lim"] = int(limit)

    rows = session.execute(text(sql), p).fetchall()
    df = pd.DataFrame(rows, columns=_BASE_COLS + ["adj_offset"])
    if limit:
        df = df.iloc[::-1].reset_index(drop=True)   # 恢复时间升序
    if df.empty:
        return None
    # 防御：物化表若有重复主键，LEFT JOIN 会把一根 K 放大成多根，
    # 下游 EMA/ATR 会被静默污染，必须 fail loud。
    if df["dt"].duplicated().any():
        n = int(df["dt"].duplicated().sum())
        raise RuntimeError(
            f"[fusion_kline] {symbol}: {BA_TABLE} 存在重复 (symbol,bucket)，"
            f"LEFT JOIN 放大出 {n} 行重复 K；先去重物化表再取数")
    for c in _PRICE_COLS:
        df[c] = df[c].astype(float)
    return df


def _segment_count(session, symbol: str) -> int:
    return int(session.execute(text(
        f"SELECT count(*) FROM {SEG_TABLE} WHERE symbol = :s AND freq = :f"
    ), {"s": symbol, "f": FREQ_SEG}).scalar() or 0)


def _resolve_offsets(session, symbol: str, df: pd.DataFrame,
                     context: str, strict: bool) -> pd.Series:
    """物化列优先，尾部缺失用 roll_segment 现场补，都拿不到则 fail-loud。"""
    off = pd.to_numeric(df["adj_offset"], errors="coerce").astype(float)
    miss = off.isna()
    n_miss = int(miss.sum())
    if n_miss:
        logger.warning(
            f"[fusion_kline/{context}] {symbol}: {n_miss}/{len(df)} 根桶在 "
            f"{BA_TABLE} 无 adj_offset，改用 {SEG_TABLE}(freq={FREQ_SEG}) 现场计算补齐"
        )
        try:
            from app.data.back_adjust import apply_back_adjust
            probe = df.loc[miss, _BASE_COLS].copy()
            calc = apply_back_adjust(probe, symbol, FREQ_SEG,
                                     session=session, time_col="dt")
            off.loc[miss] = pd.to_numeric(
                calc["adj_offset"], errors="coerce").to_numpy()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fusion_kline/{context}] {symbol}: 现场复权失败: {e}")
            if strict:
                raise

    n_seg = _segment_count(session, symbol)
    if n_seg == 0:
        msg = (f"[fusion_kline/{context}] {symbol}: {SEG_TABLE} 无 freq={FREQ_SEG} "
               f"换月段，后复权维度不可信（P0-2：段表停在 2026-10-30，"
               f"10-30 后 PX/AG/BR/PF/PR 的 change_flag 未成段）")
        logger.error(msg)
        if strict:
            raise RuntimeError(msg)

    if strict and off.isna().any():
        raise RuntimeError(
            f"[fusion_kline/{context}] {symbol}: 仍有 {int(off.isna().sum())} 根桶"
            f"取不到 adj_offset，拒绝静默按 0 偏移产出后复权 K 线")
    return off.fillna(0.0)


def read_fusion_kline(session, symbol: str, limit: int | None = None, *,
                      src: str = "bar_60m",
                      start: date | datetime | str | None = None,
                      end: date | datetime | str | None = None,
                      context: str = "live",
                      strict: bool = True) -> pd.DataFrame | None:
    """回测与实盘共用的唯一取数入口。

    Args:
        session: SQLAlchemy Session / Connection
        symbol:  品种主连符号（如 RB888）
        limit:   实盘取最近 N 根（与 start/end 互斥）
        src:     "bar_60m" 原始维度 / "bar_60m_ba" 同一条 K 的后复权维度
        start, end: 回测时间窗（含端点）
        context: "live" | "backtest" | "audit"，仅用于日志标注，不影响数值
        strict:  后复权来源不完整时是否抛错（默认 True，验收/生产都建议保持）

    Returns:
        DataFrame 列 `dt/open/high/low/close`（按 dt 升序）；后复权维度额外带
        `adj_offset`。无数据返回 **None**（与旧 `_read_src` 契约一致）。
    """
    dim = normalize_src(src)
    if context not in _CONTEXTS:
        raise ValueError(f"未知 context={context!r}，可选 {sorted(_CONTEXTS)}")
    if limit and (start is not None or end is not None):
        logger.warning(
            f"[fusion_kline/{context}] {symbol}: limit 与 start/end 同时给出，"
            f"按 live 语义忽略时间窗")
        start = end = None

    df = _fetch(session, symbol, limit, start, end)
    if df is None:
        return None

    out = df[_BASE_COLS].copy()
    if dim == "back_adj":
        off = _resolve_offsets(session, symbol, df, context, strict)
        for c in _PRICE_COLS:
            out[c] = out[c] + off.to_numpy()
        out["adj_offset"] = off.to_numpy()

    # 元数据供执行层 / 验收脚本识别价格空间（pandas attrs 在纯列操作中保留）
    out.attrs.update({
        "kline_src": RAW_TABLE,
        "kline_dim": dim,
        "kline_context": context,
        "symbol": symbol,
    })
    return out


def verify_same_kline(session, symbol: str, start=None, end=None,
                      tol: float = 1e-6) -> dict:
    """验收断言：物化后复权维度确实是 `bar_60m` 同一批桶 + 常量偏移。

    检查项：
      * 桶集合一致（L1 有 BA 无 / BA 有 L1 无，均应为 0）
      * 恒等式 `ba.close - l1.close = adj_offset`（容差 tol）
      * volume 一致（后复权只改价格，不改成交量）
    """
    p: dict = {"sym": symbol}

    def _win(prefix: str) -> str:
        cond = ""
        if start is not None:
            cond += f" AND {prefix}bucket >= :s"
            p["s"] = pd.Timestamp(start)
        if end is not None:
            cond += f" AND {prefix}bucket <= :e"
            p["e"] = pd.Timestamp(end)
        return cond

    win_l = _win("l.")
    sql = (
        "SELECT count(*) AS n_l1, count(b.bucket) AS n_matched, "
        "  count(*) FILTER (WHERE b.bucket IS NULL) AS n_missing_ba, "
        "  max(abs((b.close - l.close) - b.adj_offset)) AS maxdiff, "
        "  count(*) FILTER (WHERE abs((b.close - l.close) - b.adj_offset) > :tol) AS n_bad, "
        "  count(*) FILTER (WHERE b.volume IS DISTINCT FROM l.volume) AS n_vol_diff "
        f"FROM {RAW_TABLE} l LEFT JOIN {BA_TABLE} b "
        "  ON b.symbol = l.symbol AND b.bucket = l.bucket "
        f"WHERE l.symbol = :sym{win_l}"
    )
    p["tol"] = tol
    r = session.execute(text(sql), p).mappings().one()
    d = dict(r)
    d["maxdiff"] = float(d["maxdiff"]) if d["maxdiff"] is not None else None

    win_b = _win("b.")
    p2 = {"sym": symbol}
    if start is not None:
        p2["s"] = pd.Timestamp(start)
    if end is not None:
        p2["e"] = pd.Timestamp(end)
    d["n_orphan_ba"] = int(session.execute(text(
        f"SELECT count(*) FROM {BA_TABLE} b WHERE b.symbol = :sym{win_b} "
        f"AND NOT EXISTS (SELECT 1 FROM {RAW_TABLE} l "
        f"  WHERE l.symbol = b.symbol AND l.bucket = b.bucket)"
    ), p2).scalar() or 0)

    d["n_seg_min60"] = _segment_count(session, symbol)
    d["symbol"] = symbol
    d["ok"] = bool(
        d["n_missing_ba"] == 0 and d["n_orphan_ba"] == 0
        and d["n_bad"] == 0 and d["n_vol_diff"] == 0
        and d["n_l1"] > 0
    )
    return d
