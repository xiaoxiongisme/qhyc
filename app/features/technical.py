"""技术类特征（PRD §5.1）：收益率、波动率、ATR、成交量变化率

窗口均以"根数"表达（§4.4），由 config.predict.windows 注入。
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


def ret_series(close: pd.Series) -> pd.Series:
    """单期涨跌幅 %（⑩：口径基于权威口径序列的 close）"""
    return close.pct_change() * 100.0


def pct_change_n(close: pd.Series, n: int) -> float | None:
    """前 n 根累计涨跌幅 %（ret5 / ret20）"""
    if len(close) <= n:
        return None
    prev, cur = close.iloc[-1 - n], close.iloc[-1]
    if prev is None or prev == 0 or pd.isna(prev):
        return None
    return float((cur - prev) / prev * 100.0)


def rolling_vol(rets: pd.Series, n: int) -> float | None:
    """近 n 根收益率标准差（波动率聚类特征）"""
    if len(rets) < n:
        return None
    return float(rets.iloc[-n:].std(ddof=1))


def atr14(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> float | None:
    """平均真实波幅（相对值 %，消除价格量纲）"""
    if len(close) < n + 1:
        return None
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(n).mean().iloc[-1]
    last = close.iloc[-1]
    if atr is None or pd.isna(atr) or last == 0:
        return None
    return float(atr / last * 100.0)


def volume_change(volume: pd.Series, n: int = 5) -> float | None:
    """成交量变化率：今日量 / n 根均量 - 1"""
    if volume is None or len(volume) < n + 1:
        return None
    ma = volume.iloc[-n - 1 : -1].mean()
    last = volume.iloc[-1]
    if ma is None or pd.isna(ma) or ma == 0 or pd.isna(last):
        return None
    return float(last / ma - 1.0)


def ret_window_stats(rets: Sequence[float], n: int) -> dict:
    """特征窗口统计（供日志 / 看板）"""
    arr = np.asarray(rets[-n:], dtype=float)
    if arr.size == 0:
        return {}
    return {
        f"ret_mean_{n}": float(np.nanmean(arr)),
        f"ret_std_{n}": float(np.nanstd(arr, ddof=1)) if arr.size > 1 else None,
        f"win_rate_{n}": float((arr > 0).mean()),
    }
