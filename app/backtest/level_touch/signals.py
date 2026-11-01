# -*- coding: utf-8 -*-
"""level_touch · 信号生成（多头 + 空头对称）。

三类假设（均由点位触达理论推出），每类含多头(L)与空头(S)对称版本：

S1 均值回归：价格触及一个历史上被高频触及（高 z）的价位带后收回。
    - L：跌入强支撑带并收回 → 做多（博反弹）
    - S：涨入强阻力带并收回 → 做空（博回落）
S2 突破/回踩动量延续：
    - L：近期创 ATH（或回踩至 ATH 带内收回）→ 做多（上行延续）
    - S：近期创 ATL（或回踩至 ATL 带内收回）→ 做空（下行延续）
S3 难回价位（直接检验用户 Q3）：出现 ≥drawdown_thr 的「坑」，自坑底回升
    ≥recover_gain，且拟合漂移下「不回坑底(卖价)」概率 ≥ 阈值：
    - L：正向漂移 → 做多（坑底难回）
    - S：负向漂移 → 做空（峰位难回）

性能：不再每 bar 扫描整个价格网格，而是用 backtest 预计算的「强价位有序列表」
(strong_levels) 做 O(log n) 二分查找，使分钟级回测可行。

决策时点：t 日收盘（使用 ≤ t-1 的滚动触达图 / ATH(ATL) / 漂移）。
所有成交在 t+1 开盘（见 backtest.py），本模块只产出「方向 + 止损/止盈距离 +
理由」。stop_dist / target_dist 始终为**正的距离量**，由 backtest 按方向施加符号。
"""
from __future__ import annotations

import numpy as np

from .engine import recurrence_no_return


def generate_signal(ctx: dict) -> dict:
    """根据滚动状态产出信号。

    ctx 字段：
      price, low_t, high_t, tick, atr, mu, sigma, t
      strong_levels, strong_z   : 显著价位有序数组（按价位升序）与其 z
      ath_price, ath_day, atl_price, atl_day : 累积 ATH/ATL 及首现下标
      roll_max, roll_min        : 滚动峰/谷序列（长度 n），用 [t] 取当前
      holding_side              : 'L' | 'S' | None（当前持仓方向）
      s2_lookback_bars, s3_window_bars : bar 级缩放后的回看窗
      cfg

    返回：{'side': 'L'|'S'|None, 'stop_dist': float, 'target_dist': float,
           'reason': str, 'exit': bool}
    """
    cfg = ctx["cfg"]
    price = float(ctx["price"])
    low_t = float(ctx["low_t"])
    high_t = float(ctx["high_t"])
    tick = float(ctx["tick"])
    atr = float(ctx["atr"])
    mu = float(ctx["mu"])
    sigma = float(ctx["sigma"])
    t = int(ctx["t"])
    tol = cfg.tol_ticks * tick

    levels = ctx["strong_levels"]
    zlev = ctx["strong_z"]
    # 当前价下方最近强支撑 / 上方最近强阻力（O(log n)）
    sup = res = None
    if levels.size:
        j = int(np.searchsorted(levels, price))
        if j - 1 >= 0:
            sup = float(levels[j - 1])
        if j < levels.size:
            res = float(levels[j])

    out = {"side": None, "stop_dist": 0.0, "target_dist": 0.0,
           "reason": "", "exit": False}

    # ---------- 持仓中的离场 ----------
    side = ctx.get("holding_side")
    if side == "L" and res is not None:
        if price >= res - tol:                      # 触碰强阻力 → 兑现
            out["exit"] = True
            out["reason"] = f"多仓触碰强阻力 {res:.1f}"
            return out
    elif side == "S" and sup is not None:
        if price <= sup + tol:                      # 触碰强支撑 → 兑现
            out["exit"] = True
            out["reason"] = f"空仓触碰强支撑 {sup:.1f}"
            return out
    if side:
        return out

    s2_lb = int(ctx["s2_lookback_bars"])

    # ---------- S1 支撑带反弹（多）----------
    if sup is not None and low_t <= sup + tol and price >= sup:
        stop_price = sup - cfg.stop_atr_mult * atr
        out["side"] = "L"
        out["stop_dist"] = max(price - stop_price, 0.5 * atr)
        out["target_dist"] = cfg.target_atr_mult * atr
        out["reason"] = f"S1 支撑反弹 @{sup:.1f}"
        return out

    # ---------- S1 阻力带回落（空，对称）----------
    if cfg.allow_short and res is not None and high_t >= res - tol and price <= res:
        stop_price = res + cfg.stop_atr_mult * atr
        out["side"] = "S"
        out["stop_dist"] = max(stop_price - price, 0.5 * atr)
        out["target_dist"] = cfg.target_atr_mult * atr
        out["reason"] = f"S1 阻力回落 @{res:.1f}"
        return out

    # ---------- S2 ATH 突破/回踩（多）----------
    ath = float(ctx["ath_price"])
    ath_day = int(ctx["ath_day"])
    if ath_day >= t - s2_lb:
        retest_band = ath - cfg.s2_retest_tol_atr * atr
        if price > ath or (low_t <= ath + tol and price >= retest_band):
            stop_price = ath - cfg.stop_atr_mult * atr
            out["side"] = "L"
            out["stop_dist"] = max(price - stop_price, 0.5 * atr)
            out["target_dist"] = cfg.target_atr_mult * atr
            out["reason"] = f"S2 ATH 突破/回踩 @{ath:.1f}"
            return out

    # ---------- S2 ATL 突破/回踩（空，对称）----------
    atl = float(ctx["atl_price"])
    atl_day = int(ctx["atl_day"])
    if cfg.allow_short and atl_day >= t - s2_lb:
        retest_band_s = atl + cfg.s2_retest_tol_atr * atr
        if price < atl or (high_t >= atl - tol and price <= retest_band_s):
            stop_price = atl + cfg.stop_atr_mult * atr
            out["side"] = "S"
            out["stop_dist"] = max(stop_price - price, 0.5 * atr)
            out["target_dist"] = cfg.target_atr_mult * atr
            out["reason"] = f"S2 ATL 突破/回踩 @{atl:.1f}"
            return out

    # ---------- S3 难回买价（多）/ 难回卖价（空）----------
    # 坑结构由 backtest 用 rolling_peak_trough 预计算：s3_peak_arr[t]=窗口内最高
    # 收盘，s3_trough_arr[t]=该峰值之后的最低收盘（时间有序，真正的回撤-恢复）。
    peak_arr = ctx.get("s3_peak_arr")
    trough_arr = ctx.get("s3_trough_arr")
    if peak_arr is not None and trough_arr is not None and (t - 1) < len(peak_arr):
        peak = float(peak_arr[t - 1])
        trough = float(trough_arr[t - 1])
        if peak > 0 and trough > 0:
            dd = (peak - trough) / peak
            gain = price / trough - 1.0
            if dd >= cfg.s3_drawdown_thr and gain >= cfg.s3_recover_gain:
                if mu > 0:
                    p = recurrence_no_return(mu, sigma, gain)
                    if p >= cfg.s3_no_return_p_thr:
                        stop_price = trough - cfg.stop_atr_mult * atr
                        out["side"] = "L"
                        out["stop_dist"] = max(price - stop_price, 0.5 * atr)
                        out["target_dist"] = cfg.target_atr_mult * atr
                        out["reason"] = (f"S3 难回买价 坑底={trough:.1f} "
                                         f"回升={gain*100:.1f}% 不回概率={p*100:.0f}%")
                        return out
                elif cfg.allow_short and mu < 0:
                    p = recurrence_no_return(abs(mu), sigma, gain)
                    if p >= cfg.s3_no_return_p_thr:
                        stop_price = peak + cfg.stop_atr_mult * atr
                        out["side"] = "S"
                        out["stop_dist"] = max(stop_price - price, 0.5 * atr)
                        out["target_dist"] = cfg.target_atr_mult * atr
                        out["reason"] = (f"S3 难回卖价 峰位={peak:.1f} "
                                         f"回升={gain*100:.1f}% 不回概率={p*100:.0f}%")
                        return out
    return out
