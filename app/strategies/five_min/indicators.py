# -*- coding: utf-8 -*-
"""5m 指标层：EMA21/60 + MACD12/27/9 + Wilder ATR14 + 日线方向门（CL-20261121-04）。

与本地研究基线 `bt_engine.prepare_base` 逐口径对齐，但**不 import 研究目录**：
* EMA 用递推 ``alpha=2/(n+1)``（与 pandas ewm(span=n, adjust=False) 等价）；
* MACD = EMA12 - EMA27，DEA = EMA(MACD,9)，hist = DIF - DEA；
* ATR 用 Wilder 平滑；
* 日线方向只取**严格早于该 5m bar 交易日**的最后一根日线（防前视），
  夜盘 00:00–03:00 归前一交易日。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

DEFAULT_CFG: dict[str, Any] = {
    "ema_fast": 21, "ema_slow": 60,
    "macd_fast": 12, "macd_slow": 27, "macd_signal": 9,
    "atr_n": 14, "daily_ema_fast": 20, "daily_ema_slow": 60,
    "hist_th": 0.10, "session": (9, 15),
    "risk_r": 500.0, "sl_atr": 3.0, "tp_atr": 7.5,
    "families": ("trend_pullback", "macd_cross"),
}


def ema(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    alpha = 2.0 / (n + 1.0)
    seed = x[0]
    out[0] = seed
    for i in range(1, len(x)):
        v = x[i]
        seed = (v * alpha + seed * (1 - alpha)) if np.isfinite(v) else seed
        out[i] = seed
    return out


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    m = len(close)
    out = np.full(m, np.nan)
    if m <= n:
        return out
    tr = np.empty(m)
    tr[0] = high[0] - low[0]
    for i in range(1, m):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    prev = float(np.nanmean(tr[1:n + 1]))
    out[n] = prev
    for i in range(n + 1, m):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def trading_date(ts: pd.Timestamp) -> pd.Timestamp:
    """夜盘跨零点归前一自然日；白盘（本策略只用 9–15）不变。"""
    d = ts.normalize()
    return d - pd.Timedelta(days=1) if ts.hour < 3 else d


def attach_indicators(bars: pd.DataFrame, daily: pd.DataFrame,
                      cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """返回带指标的副本；调用方的原始 DataFrame 不被修改。"""
    p = dict(DEFAULT_CFG)
    if cfg:
        p.update(cfg)
    d = bars.copy().reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        d[col] = pd.to_numeric(d[col], errors="coerce").astype(float)
    d = d.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if d.empty:
        return d
    c = d["close"].to_numpy(float)
    ef = ema(c, p["ema_fast"])
    es = ema(c, p["ema_slow"])
    dif = ema(c, p["macd_fast"]) - ema(c, p["macd_slow"])
    dea = ema(dif, p["macd_signal"])
    d["ema_f"], d["ema_s"] = ef, es
    d["dif"], d["dea"], d["hist"] = dif, dea, dif - dea
    d["atr"] = wilder_atr(d["high"].to_numpy(), d["low"].to_numpy(), c, p["atr_n"])

    if daily is None or daily.empty:
        d["daily_dir"], d["daily_pos"], d["tdate"] = 0, -1, None
        return d
    dc = pd.to_numeric(daily["close"], errors="coerce").to_numpy(float)
    dcol = "date" if "date" in daily.columns else (
        "trade_date" if "trade_date" in daily.columns else "dt")
    ddates = pd.to_datetime(daily[dcol]).to_numpy(dtype="datetime64[ns]")
    d_f = ema(dc, p["daily_ema_fast"])
    d_s = ema(dc, p["daily_ema_slow"])
    dd = np.zeros(len(dc), dtype=np.int8)
    dd[(dc > d_s) & (d_f > d_s)] = 1
    dd[(dc < d_s) & (d_f < d_s)] = -1
    tdates = np.array([trading_date(pd.Timestamp(t)).to_datetime64() for t in d["dt"]],
                      dtype="datetime64[ns]")
    # side='left' ⇒ 只取严格早于本交易日的日线；当日未收盘不会提前入模。
    pos = np.searchsorted(ddates, tdates, side="left") - 1
    d["daily_dir"] = np.where(pos >= 0, dd[np.clip(pos, 0, None)], 0).astype(np.int8)
    d["daily_pos"] = pos
    d["tdate"] = tdates
    return d
