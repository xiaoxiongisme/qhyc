"""数据归一化工具"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

import pandas as pd

# 列名映射：akshare 原始列 -> 库内列
AK_DAILY_COL_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "开盘价": "open",
    "最高": "high",
    "最高价": "high",
    "最低": "low",
    "最低价": "low",
    "收盘": "close",
    "收盘价": "close",
    "结算价": "settle",
    "动态结算价": "settle",   # futures_main_sina 返回的结算价列名
    "成交量": "volume",
    "成交额": "amount",
    "持仓量": "oi",
}


def normalize_ak_daily(df: pd.DataFrame, symbol: str) -> list[dict]:
    """把 akshare 原始 DataFrame 转为入库 dict 列表"""
    if df is None or df.empty:
        return []
    # 兼容中英文列名
    rename_map = {}
    for src, dst in AK_DAILY_COL_MAP.items():
        if src in df.columns:
            rename_map[src] = dst
    if not rename_map:
        # 已经是英文列
        rename_map = {c: c for c in df.columns}
    out = df.rename(columns=rename_map)
    keep = ["trade_date", "open", "high", "low", "close", "settle", "volume", "amount", "oi"]
    out = out[[c for c in keep if c in out.columns]].copy()

    rows: list[dict] = []
    for _, r in out.iterrows():
        td = r.get("trade_date")
        if pd.isna(td):
            continue
        if isinstance(td, pd.Timestamp):
            td = td.date()
        elif isinstance(td, str):
            td = date.fromisoformat(td)
        row = {
            "symbol": symbol,
            "trade_date": td,
            "open": _to_decimal(r.get("open")),
            "high": _to_decimal(r.get("high")),
            "low": _to_decimal(r.get("low")),
            "close": _to_decimal(r.get("close")),
            "settle": _to_decimal(r.get("settle")),
            "volume": _to_int(r.get("volume")),
            "amount": _to_decimal(r.get("amount")),
            "oi": _to_int(r.get("oi")),
            "src": "akshare",
        }
        rows.append(row)
    return rows


def attach_returns(rows: list[dict]) -> list[dict]:
    """入库前计算 ret_close / ret_settle（基于排序后的 close/settle）"""
    if not rows:
        return rows
    rows_sorted = sorted(rows, key=lambda x: x["trade_date"])
    prev_close: Decimal | None = None
    prev_settle: Decimal | None = None
    closes: list[Decimal | None] = []
    settles: list[Decimal | None] = []
    for r in rows_sorted:
        close = _to_decimal(r.get("close"))
        settle = _to_decimal(r.get("settle"))
        rc = None
        rs = None
        if close is not None and prev_close not in (None, 0):
            rc = (close - prev_close) / prev_close * Decimal(100)
        if settle is not None and prev_settle not in (None, 0):
            rs = (settle - prev_settle) / prev_settle * Decimal(100)
        r["ret_close"] = _round6(rc)
        r["ret_settle"] = _round6(rs)
        prev_close = close
        prev_settle = settle
        closes.append(close)
        settles.append(settle)

    # ret5 / ret20：以根数表达（§4.4）
    n = len(rows_sorted)
    for i, r in enumerate(rows_sorted):
        r["ret5"] = _pct_change(closes, i, 5)
        r["ret20"] = _pct_change(closes, i, 20)
    return rows


def _pct_change(closes: list, i: int, window: int) -> Decimal | None:
    if i < window:
        return None
    prev = closes[i - window]
    cur = closes[i]
    if prev in (None, 0) or cur is None:
        return None
    return _round6((cur - prev) / prev * Decimal(100))


def _to_decimal(v) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None


def _round6(v: Decimal | None) -> Decimal | None:
    if v is None:
        return None
    return v.quantize(Decimal("0.000001"))


__all__ = [
    "normalize_ak_daily",
    "attach_returns",
]