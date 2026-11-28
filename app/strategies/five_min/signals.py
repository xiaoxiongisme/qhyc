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


def _family_signals(ctx: pd.DataFrame, family: str,
                    hist_th: float = 0.10) -> tuple[np.ndarray, np.ndarray]:
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
    elif family == "hist_momentum":
        # CL-20261128-01 · S2：复现研究 hist_up010 / hist_dn010
        #   ha = hist/a14（无方向），触发 = 当根>th 且前根<=th（上穿）。
        # 数据列由 indicators.attach_indicators 以 causal 口径生成。
        if "hist_atr_raw" not in ctx:
            raise ValueError("[fail-loud] hist_momentum 需要 hist_atr_raw 列")
        ha = ctx["hist_atr_raw"].to_numpy(float)
        th = float(hist_th)
        lu = cross_up(ha, np.full(len(ha), th))
        sd = cross_dn(ha, np.full(len(ha), -th))
    else:
        raise ValueError(
            f"未知信号族 {family!r}（仅 trend_pullback/macd_cross/hist_momentum）")
    return valid & lu & (direction == 1), valid & sd & (direction == -1)


def _scan_s2(ctx: pd.DataFrame, p: dict[str, Any]) -> list[Signal]:
    """CL-20261128-01 · S2 信号：hist_up010 ∧ 日线多 ∧ 夜盘 ∧ 高波动。

    精确复现 combo_final_check.CORE[1] + combo_enumerate.build_triggers/build_filters：
      * hist_up010 = ha>th 且前根 ha<=th（ha=hist/a14，无方向，causal）；
      * dir_long   = daily_dir>0（t-1 日线门）；
      * sess_night = session=='night'（研究口径 hour>=20，凌晨归 am）；
      * atr_hi     = atr_pct_rank>0.7（50 根分位，causal）；
      * 做多用 hist_up010；做空镜像 hist_dn010（同族反向，供对称校验）。
    fail-loud：缺任一必要列直接抛错，绝不静默退化为别的口径。
    """
    need = ("hist_atr_raw", "daily_dir", "session", "atr_pct_rank", "atr", "dt")
    missing = [c for c in need if c not in ctx]
    if missing:
        raise ValueError(f"[fail-loud] S2 缺必要列 {missing}，请先 attach_indicators")

    n = len(ctx)
    ha = ctx["hist_atr_raw"].to_numpy(float)
    th = float(p["hist_th"])
    direction = ctx["daily_dir"].to_numpy()
    sess = ctx["session"].to_numpy()
    rank = ctx["atr_pct_rank"].to_numpy(float)
    atr = ctx["atr"].to_numpy(float)
    valid = np.isfinite(atr) & (atr > 0) & np.isfinite(ha)
    if p.get("atr_rank_min") is not None:
        valid = valid & np.isfinite(rank) & (rank > float(p["atr_rank_min"]))
    # 时段门：night 仅 hour>=20；其他会话值一律拒绝（S2 只做夜盘）。
    session_cfg = p.get("session")
    if session_cfg == "night":
        valid = valid & (sess == "night")
    else:
        raise ValueError(f"[fail-loud] S2 仅支持 session='night'，收到 {session_cfg!r}")

    lu = (ha > th) & np.r_[True, ha[:-1] <= th]   # hist_up010
    sd = (ha < -th) & np.r_[True, ha[:-1] >= -th]  # hist_dn010（对称镜像）
    dts = list(ctx["dt"])
    out: list[Signal] = []
    for i in np.where(lu & valid & (direction == 1))[0]:
        out.append(Signal(int(i), 1, float(ha[i]), "hist_momentum", dts[int(i)]))
    for i in np.where(sd & valid & (direction == -1))[0]:
        out.append(Signal(int(i), -1, float(-ha[i]), "hist_momentum", dts[int(i)]))
    out.sort(key=lambda s: s.index)
    return out


def scan_signals(ctx: pd.DataFrame, cfg: dict[str, Any] | None = None) -> list[Signal]:
    """返回按 bar 时间排序的候选事件；同一 bar 多族命中只保留第一条。"""
    p = dict(DEFAULT_CFG)
    if cfg:
        p.update(cfg)
    n = len(ctx)
    if n == 0:
        return []

    # CL-20261128-01 · S2 独立路径：仅在显式配置 hist_momentum 族时启用。
    # 与默认两族路径完全隔离，默认行为逐字保留（守卫/既有测试钉死）。
    if "hist_momentum" in p["families"]:
        return _scan_s2(ctx, p)

    le_any = np.zeros(n, dtype=bool)
    se_any = np.zeros(n, dtype=bool)
    fam_of_le: dict[int, str] = {}
    fam_of_se: dict[int, str] = {}
    for fam in p["families"]:
        le, se = _family_signals(ctx, fam, float(p["hist_th"]))
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
