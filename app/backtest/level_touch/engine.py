# -*- coding: utf-8 -*-
"""level_touch · 纯数学引擎（不依赖数据库，可单测）。

核心对象：价格点位触达图 (touch map)
------------------------------------
把价格轴离散成 bin。对某价位 p，第 t 天日区间 [low_t, high_t] 覆盖 p
（low_t <= p <= high_t）则称「第 t 天触及 p」。

由此派生：
  * touches[p]   —— 历史触达天数（频次）
  * exp_touch[p] —— 零模型期望触达天数（每日以收盘为中心、半振幅为宽度的均匀盒）
  * z[p]         —— (touches - exp)/sqrt(exp)，显著为正=真实支撑/阻力
  * ATH / ATL    —— 历史最高/最低
  * drift / vol  —— 对数收益日漂移 μ、波动 σ（用于递归概率）
  * recurrence   —— 涨 G% 后「永不回买价」概率（一维漂移布朗闭式解）

数学依据见项目根 README 与对话记录：Tanaka 局部时间、反射原理、首达时
逆高斯分布、一维随机游走常返/瞬态性。
"""
from __future__ import annotations

import math
from collections import deque
from typing import Tuple

import numpy as np


# ---------------------------------------------------------------------------
# 价格网格
# ---------------------------------------------------------------------------
def make_grid(low: float, high: float, bin_width: float, max_bins: int = 4000):
    """返回 (edges, centers, e0)。e0 = 第一个 edge。"""
    if bin_width <= 0:
        raise ValueError("bin_width 必须为正")
    nb_raw = int(math.ceil((high - low) / bin_width)) + 1
    if nb_raw > max_bins:
        bin_width = (high - low) / (max_bins - 1)
        nb_raw = max_bins
    e0 = math.floor(low / bin_width) * bin_width
    edges = np.arange(e0, e0 + (nb_raw + 1) * bin_width, bin_width)
    centers = (edges[:-1] + edges[1:]) / 2.0
    return edges, centers, e0


def auto_bin_width(median_range: float, tick_size: float,
                   bins_per_range: float = 4.0) -> float:
    return max(tick_size, median_range / max(bins_per_range, 1e-9))


# ---------------------------------------------------------------------------
# 增量累加（回测主循环用，避免每日重算 O(T^2)）
# ---------------------------------------------------------------------------
def add_range(touches: np.ndarray, e0: float, bin_width: float,
              lo: float, hi: float) -> None:
    """把 [lo, hi] 覆盖的所有 bin 的 touches +1（就地修改）。"""
    a = int(math.floor((lo - e0) / bin_width))
    b = int(math.floor((hi - e0) / bin_width))
    a = max(a, 0)
    b = min(b, touches.shape[0] - 1)
    if b >= a >= 0:
        touches[a:b + 1] += 1


def add_box(exp: np.ndarray, e0: float, bin_width: float,
            center: float, half_width: float) -> None:
    """把以 center 为中心、半宽 half_width 的均匀盒累加到 exp（零模型期望）。"""
    lo = center - half_width
    hi = center + half_width
    a = int(math.floor((lo - e0) / bin_width))
    b = int(math.floor((hi - e0) / bin_width))
    a = max(a, 0)
    b = min(b, exp.shape[0] - 1)
    if b >= a >= 0:
        exp[a:b + 1] += 1.0


# ---------------------------------------------------------------------------
# 统计派生
# ---------------------------------------------------------------------------
def z_score(touches: np.ndarray, exp_touch: np.ndarray) -> np.ndarray:
    """z = (touches - exp)/sqrt(exp)，对 exp<=0 的 bin 置 0（无参考基准）。"""
    out = np.zeros_like(touches, dtype=float)
    m = exp_touch > 0
    out[m] = (touches[m] - exp_touch[m]) / np.sqrt(exp_touch[m])
    return out


def bin_index_of(centers: np.ndarray, e0: float, bin_width: float, price: float) -> int:
    return int(math.floor((price - e0) / bin_width))


def fit_drift_vol(log_returns: np.ndarray) -> Tuple[float, float]:
    """日对数收益的漂移 μ 与波动 σ（样本估计）。返回 (mu, sigma)。"""
    r = log_returns[np.isfinite(log_returns)]
    if r.size < 2:
        return 0.0, 0.0
    mu = float(np.mean(r))
    sigma = float(np.std(r, ddof=1))
    return mu, sigma


def recurrence_no_return(mu: float, sigma: float, gain: float) -> float:
    """对数价 X_t 带漂移布朗：从买价上涨 gain 后，永不回买价的概率。

        P = 1 - exp(-2 * mu * ln(1+gain) / sigma^2),   mu > 0
        mu <= 0 时恒为 0（一维随机游走常返，必然回归）。

    :param gain: 涨幅（0.2 = +20%）。
    """
    if sigma <= 0 or gain <= -1:
        return float("nan")
    a = math.log1p(gain)
    if mu <= 0:
        return 0.0
    return 1.0 - math.exp(-2.0 * mu * a / (sigma ** 2))


def gain_for_threshold(mu: float, sigma: float, p_no_return: float) -> float:
    """反解：要达到「不回买价概率 = p」所需涨幅 gain。"""
    if mu <= 0 or sigma <= 0 or p_no_return <= 0 or p_no_return >= 1:
        return float("nan")
    a = -0.5 * (sigma ** 2) * math.log1p(-p_no_return) / mu
    return math.exp(a) - 1.0


def support_zones(centers: np.ndarray, z: np.ndarray, touches: np.ndarray,
                  z_thr: float, min_touches: int):
    """返回显著支撑/阻力价位的 (price, z, touches)，按 |z| 降序。"""
    m = (np.abs(z) >= z_thr) & (touches >= min_touches)
    idx = np.where(m)[0]
    order = idx[np.argsort(-np.abs(z[idx]))]
    return [(float(centers[i]), float(z[i]), int(touches[i])) for i in order]


def singleton_levels(centers: np.ndarray, touches: np.ndarray, value: int = 1):
    """返回触达次数恰为 value 的价位列表（稀有层级）。"""
    idx = np.where(touches == value)[0]
    return [float(centers[i]) for i in idx]


def strong_levels(centers: np.ndarray, z: np.ndarray, touches: np.ndarray,
                  z_thr: float, min_touches: int):
    """返回显著价位（z>=z_thr 且 touches>=min_touches）的**有序**价位与对应 z。

    仅取**正 z**：正 z = 价格反复触及的真实支撑/阻力；负 z 表示价格系统性绕开
    该价位（伪支撑/阻力），不应触发均值回归入场。这与 signals.py 旧版
    `z >= thr` 掩码语义一致。

    用于把 O(nb) 的全网格掩码替换为 O(log n) 的二分查找，使分钟级回测不再每
    bar 扫描整个价格网格。返回 (levels, z_levels)，均按价位升序。
    """
    m = (z >= z_thr) & (touches >= min_touches)
    idx = np.where(m)[0]
    if idx.size == 0:
        return (np.empty(0), np.empty(0))
    levels = centers[idx]
    order = np.argsort(levels)
    return levels[order], z[idx][order]


def _rolling_mean_std(x: np.ndarray, window: int):
    """对一维序列计算滚动（或扩展）均值/标准差，O(n) 经 cumsum 实现。

    前 window 个用扩展窗口（从 0 开始），之后为标准滚动窗口。返回 (mu, sigma)
    长度同 x。用于把漂移/波动估计从「每 bar 切片」的 O(n*window) 降到 O(n)，
    是分钟级回测可行化的关键优化。
    """
    x = np.asarray(x, dtype=float)
    n = x.size
    if n == 0:
        return (np.empty(0), np.empty(0))
    w = max(1, int(window))
    csum = np.concatenate(([0.0], np.cumsum(x)))
    csumsq = np.concatenate(([0.0], np.cumsum(x * x)))
    # 起始下标：扩展窗口用 0，滚动窗口用 i-w
    starts = np.clip(np.arange(n) - w + 1, 0, n)
    cnt = (np.arange(n) + 1 - starts).astype(float)
    s = csum[1:n + 1] - csum[starts]
    sq = csumsq[1:n + 1] - csumsq[starts]
    mu = s / cnt
    var = sq / cnt - mu * mu
    var = np.clip(var, 0.0, None)
    sigma = np.sqrt(var)
    return mu, sigma


def cumulative_extreme(highs: np.ndarray, lows: np.ndarray):
    """返回累积 ATH(高) 与 ATL(低) 序列，O(n)。供回测在循环外一次性算出。"""
    ath = np.maximum.accumulate(np.asarray(highs, dtype=float))
    atl = np.minimum.accumulate(np.asarray(lows, dtype=float))
    return ath, atl


def rolling_peak_trough(x: np.ndarray, window: int):
    """对序列求每个 t 的「窗口峰值」与「该峰值之后的谷底」，O(n)。

    语义：peak[t] = max(x[t-W+1 .. t])，trough[t] = min(x[peak_idx[t] .. t])，
    即先取窗口内最高收盘，再取其后的最低收盘 —— 这正是 S3「坑」的时间有序结构
    （峰值→其后回撤→恢复），而非窗口内无关联的 max/min。用两个单调队列实现，
    每个下标进出 O(1)，整体 O(n)，使分钟级回测也可行。

    返回 (peak_arr, trough_after_peak_arr)。
    """
    x = np.asarray(x, dtype=float)
    n = x.size
    peak = np.empty(n)
    trough = np.empty(n)
    if n == 0:
        return peak, trough
    W = max(2, int(window))
    D = deque()   # 单调减：D[0] 为窗口内最大收盘的下标
    M = deque()   # 单调增：M[0] 为 [peak_idx, t] 内最小收盘的下标
    for t in range(n):
        while D and D[0] <= t - W:
            D.popleft()
        while D and x[D[-1]] <= x[t]:
            D.pop()
        D.append(t)
        pi = D[0]
        while M and M[0] < pi:
            M.popleft()
        while M and x[M[-1]] >= x[t]:
            M.pop()
        M.append(t)
        peak[t] = x[pi]
        trough[t] = x[M[0]]
    return peak, trough


# ---------------------------------------------------------------------------
# 路径拓扑（Reeb/Morse 视角）：单调段与临界点
# ---------------------------------------------------------------------------
def topology_summary(price: np.ndarray) -> dict:
    """把价格序列视为连续映射，统计单调段数与局部极值（用户要标出的每日高低点）。"""
    x = np.asarray(price, dtype=float)
    n = x.size
    if n < 3:
        return {"n_monotonic_runs": 0, "n_local_max": 0, "n_local_min": 0,
                "n_critical": 0}
    n_local_max = int(np.sum((x[1:-1] > x[:-2]) & (x[1:-1] > x[2:])))
    n_local_min = int(np.sum((x[1:-1] < x[:-2]) & (x[1:-1] < x[2:])))
    # 单调段：相邻同号变化归一段
    diff = np.diff(x)
    sign = np.sign(diff)
    runs = 1 + int(np.sum(sign[1:] != sign[:-1]))
    return {"n_monotonic_runs": runs, "n_local_max": n_local_max,
            "n_local_min": n_local_min, "n_critical": n_local_max + n_local_min}
