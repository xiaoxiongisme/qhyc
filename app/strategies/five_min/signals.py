# -*- coding: utf-8 -*-
"""5m 信号层（缺口1）：两族事件化信号 + 方向化 hist_atr + 白盘门（CL-20261121-04）。

复现研究引擎 `strategy_engine.signals` 口径：
* trend_pullback : close 上穿 EMA21 且 EMA21>EMA60 且 hist>0（空头镜像）；
* macd_cross     : DIF 上穿 DEA 且 EMA21>EMA60（空头镜像）；
* 两族均要求日线方向一致（daily_dir，已由 indicators 做 t-1 门）；
* 事件化：只在条件**首次出现**的那根 bar 记候选，连续状态不重复入场；
* hist_atr = sd*hist/ATR > 0.10（sd=+1 多 / -1 空）；白盘 9–15。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from app.strategies.five_min.indicators import DEFAULT_CFG


@dataclass(frozen=True)
class Signal:
    index: int
    side: int                 # +1 多 / -1 空
    hist_atr: float
    family: str
    dt: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "side": self.side, "hist_atr": self.hist_atr,
                "family": self.family, "dt": str(self.dt)}


def cross_up(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.r_[False, (a[:-1] <= b[:-1]) & (a[1:] > b[1:])]


def cross_dn(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.r_[False, (a[:-1] >= b[:-1]) & (a[1:] < b[1:])]


def _family_signals(ctx: pd.DataFrame, family: str) -> tuple[np.ndarray, np.ndarray]:
    c = ctx["close"].to_numpy(float)
    ef = ctx["ema_f"].to_numpy(float)
    es = ctx["ema_s"].to_numpy(float)
    dif = ctx["dif"].to_numpy(float)
    dea = ctx["dea"].to_numpy(float)
    hist = ctx["hist"].to_numpy(float)
    direction = ctx["daily_dir"].to_numpy()
    valid = np.isfinite(es) & np.isfinite(ctx["atr"].to_numpy(float))
    if family == "trend_pullback":
        lu = cross_up(c, ef) & (ef > es) & (hist > 0)
        sd = cross_dn(c, ef) & (ef < es) & (hist < 0)
    elif family == "macd_cross":
        lu = cross_up(dif, dea) & (ef > es)
        sd = cross_dn(dif, dea) & (ef < es)
    else:
        raise ValueError(f"未知信号族 {family!r}（仅 trend_pullback/macd_cross）")
    return valid & lu & (direction == 1), valid & sd & (direction == -1)


def scan_signals(ctx: pd.DataFrame, cfg: dict[str, Any] | None = None) -> list[Signal]:
    """返回按 bar 时间排序的候选事件；同一 bar 多族命中只保留第一条。"""
    p = dict(DEFAULT_CFG)
    if cfg:
        p.update(cfg)
    n = len(ctx)
    if n == 0:
        return []
    le_any = np.zeros(n, dtype=bool)
    se_any = np.zeros(n, dtype=bool)
    fam_of_le: dict[int, str] = {}
    fam_of_se: dict[int, str] = {}
    for fam in p["families"]:
        le, se = _family_signals(ctx, fam)
        for i in np.where(le & ~le_any)[0]:
            fam_of_le[int(i)] = fam
        for i in np.where(se & ~se_any)[0]:
            fam_of_se[int(i)] = fam
        le_any |= le
        se_any |= se

    hist = ctx["hist"].to_numpy(float)
    atr = ctx["atr"].to_numpy(float)
    hours = pd.to_datetime(ctx["dt"]).dt.hour.to_numpy()
    lo, hi = p["session"]
    white = (hours >= lo) & (hours <= hi)
    valid = np.isfinite(atr) & (atr > 0) & white
    le_ev = le_any & ~np.r_[False, le_any[:-1]]
    se_ev = se_any & ~np.r_[False, se_any[:-1]]

    dts = list(ctx["dt"]) if "dt" in ctx else [None] * n
    out: list[Signal] = []
    for i in np.where(le_ev & valid & (hist / np.maximum(atr, 1e-9) > p["hist_th"]))[0]:
        out.append(Signal(int(i), 1, float(hist[i] / atr[i]),
                          fam_of_le.get(int(i), "unknown"), dts[int(i)]))
    for i in np.where(se_ev & valid & (-hist / np.maximum(atr, 1e-9) > p["hist_th"]))[0]:
        out.append(Signal(int(i), -1, float(-hist[i] / atr[i]),
                          fam_of_se.get(int(i), "unknown"), dts[int(i)]))
    out.sort(key=lambda s: s.index)
    return out


def dedupe_by_time(signals: Iterable[Signal]) -> list[Signal]:
    """同 bar 同方向去重（单持仓重放前的最后一道清洗）。"""
    seen: set[tuple[int, int]] = set()
    out = []
    for s in signals:
        k = (s.index, s.side)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out
