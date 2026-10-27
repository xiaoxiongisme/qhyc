# -*- coding: utf-8 -*-
"""level_touch · 成本感知回测（支持多周期 daily/60m/15m 与双向持仓）。

执行模型（杜绝前视）
------------------
* 决策用 t 日收盘，使用 ≤ t-1 的滚动触达图 / ATH(ATL) / 漂移。
* 成交用 t+1 日开盘（次根开盘）；止损/止盈在 t+1 日 bar 内判定
  （多单：low<=stop→以 stop 成交；high>=target→以 target 成交；否则开盘成交）。
* 成本走 app.data.cost（历史费率 + 乘数 + tick + 滑点），每笔平仓扣一次
  往返成本（cost_points_at 已含开+平+双边滑点）。成本按(品种,日期)缓存。
* 换月/涨跌停：|bar 收益率| > eff_limit_excl 的 bar 不进触达图、不作信号日。

性能（分钟级可行化的关键）
-------------------------
* ATR、滚动漂移/波动 用 cumsum 向量化（O(n)），避免 O(n·window) 的逐 bar 切片。
* z 分数与强价位列表按 z_every（日线=每 bar，分钟级=每日）重算，并用预计算的
  「强价位有序列表」做 O(log n) 查找，避免每 bar 扫描整个价格网格。
* ATH/ATL 用 O(1) 增量维护，而非每 bar np.max(highs[:t]) 的 O(n²)。

返回 Trade 列表与聚合指标（点数 + 金额）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import LevelTouchConfig, resolve_freq
from .engine import (add_box, add_range, auto_bin_width, cumulative_extreme,
                     fit_drift_vol, make_grid, rolling_peak_trough,
                     strong_levels, z_score, _rolling_mean_std)
from .signals import generate_signal


# ---------------------------------------------------------------------------
# 成本（按 品种,日期 缓存系数）
# ---------------------------------------------------------------------------
_cost_coeffs: dict = {}


def _get_coeffs(variety: str, d):
    key = (variety, d)
    if key in _cost_coeffs:
        return _cost_coeffs[key]
    from app.data.cost import cost_coefficients, main_contract_at
    contract = main_contract_at(variety, d)
    coeffs = cost_coefficients(variety, contract=contract, on_date=d)
    _cost_coeffs[key] = coeffs
    return coeffs


def _fill_cost(variety: str, dates, i: int, price: float, tick: float) -> float:
    """单笔（开或平）成本，单位=点数/手。未知成本→2 跳兜底并标注。"""
    try:
        d = pd.Timestamp(dates[i]).date()
        from app.data.cost import cost_points_at
        coeffs = _get_coeffs(variety, d)
        return float(cost_points_at(coeffs, float(price)))
    except Exception:
        return 2.0 * tick


# ---------------------------------------------------------------------------
# 向量化 ATR（cumsum 滚动均值，O(n)）
# ---------------------------------------------------------------------------
def _rolling_atr_vec(highs, lows, closes, window: int) -> np.ndarray:
    prev = np.concatenate([[closes[0]], closes[:-1]])
    tr = np.maximum(highs - lows,
                    np.abs(highs - prev),
                    np.abs(lows - prev))
    w = max(2, int(window))
    c = np.concatenate([[0.0], np.cumsum(tr)])
    n = len(tr)
    start = np.clip(np.arange(n) - w + 1, 0, n)
    cnt = (np.arange(n) + 1 - start).astype(float)
    return (c[1:] - c[start]) / cnt


def run_backtest(bars: pd.DataFrame, variety: str, cfg: LevelTouchConfig,
                 tick: float, multiplier: float) -> dict:
    fr = resolve_freq(cfg)
    bpd = fr["bars_per_day"]
    warmup = fr["warmup_bars"]
    max_hold = fr["max_hold_bars"]
    atr_w = fr["atr_window_bars"]
    s2_lb = fr["s2_ath_lookback_bars"]
    drift_w = fr["drift_window_bars"]
    s3_w = fr["s3_window_bars"]
    eff_limit = fr["eff_limit_excl"]
    z_every = fr["z_every"]

    # 清洗：分钟级数据偶含 NaN 行（缺失/异常），否则 lows.min()/highs.max()
    # 变 NaN 会让 make_grid 的 int(NaN) 崩溃。日线数据一般无 NaN，但统一兜底。
    bars = bars.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    closes = bars["close"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    opens = bars["open"].to_numpy(dtype=float)
    dates = bars["ts"].to_numpy()
    n = len(bars)
    if n < warmup + 5 or multiplier <= 0 or tick <= 0:
        return _empty_result(variety)

    logret = np.diff(np.log(closes))
    med_range = float(np.median(highs - lows))
    bw = auto_bin_width(med_range, tick, cfg.bins_per_range)
    edges, centers, e0 = make_grid(lows.min(), highs.max(), bw, cfg.max_bins)
    nb = len(centers)

    touches = np.zeros(nb)
    exp = np.zeros(nb)
    ret = np.zeros(n)
    ret[1:] = closes[1:] / closes[:-1] - 1.0
    valid = np.abs(ret) <= eff_limit
    valid[0] = True

    atr = _rolling_atr_vec(highs, lows, closes, atr_w)

    # 滚动漂移/波动（O(n)，cumsum）
    mu_series, sigma_series = _rolling_mean_std(logret, drift_w)

    # S3 坑结构：窗口内峰值 + 其后谷底（O(n)，双单调队列，时间有序）
    s3_peak_arr, s3_trough_arr = rolling_peak_trough(closes, s3_w)

    # 状态
    pos = 0                # -1 空 / 0 平 / 1 多
    side = None
    entry_price = 0.0
    stop = 0.0
    target = 0.0
    entry_i = -1
    entry_reason = ""
    pending_side = None
    pending_stop = 0.0
    pending_target = 0.0
    pending_reason = ""
    exit_pending = False

    # 累积 ATH/ATL（O(1) 增量）
    ath = float(highs[0]); ath_idx = 0
    atl = float(lows[0]); atl_idx = 0

    trades: list[dict] = []
    eq_points = np.zeros(n)
    realized = 0.0

    # z / 强价位列表缓存
    z_cache = None
    levels_cache = np.empty(0)
    zlev_cache = np.empty(0)
    last_z_t = -10 ** 9

    for t in range(1, n):
        # ---- A. 上一决策成交（t 日数据，无前视）----
        if pending_side is not None and pos == 0:
            ep = opens[t]
            entry_price = ep
            if pending_side == "L":
                stop = ep - pending_stop
                target = ep + pending_target
            else:
                stop = ep + pending_stop
                target = ep - pending_target
            pos = 1 if pending_side == "L" else -1
            side = pending_side
            entry_i = t
            entry_reason = pending_reason
            pending_side = None
        # 强制离场（信号 / 超时）
        if pos != 0 and (exit_pending or (t - entry_i) >= max_hold):
            xp = opens[t]
            cost_e = _fill_cost(variety, dates, entry_i, entry_price, tick)
            cost_x = _fill_cost(variety, dates, t, xp, tick)
            gross = (xp - entry_price) if side == "L" else (entry_price - xp)
            net = gross - cost_e - cost_x
            trades.append(dict(
                entry_date=str(pd.Timestamp(dates[entry_i]).date()),
                exit_date=str(pd.Timestamp(dates[t]).date()),
                side=side, entry=entry_price, exit=xp,
                gross=gross, cost=cost_e + cost_x, net=net,
                holding_bars=t - entry_i, reason=entry_reason,
                exit_kind="signal/timeout"))
            realized += net
            pos = 0
            side = None
            exit_pending = False

        # 持仓中 bar 内止损/止盈（保守）
        if pos != 0:
            if side == "L":
                if lows[t] <= stop:
                    xp = stop
                elif highs[t] >= target:
                    xp = target
                else:
                    xp = None
            else:  # 空
                if highs[t] >= stop:
                    xp = stop
                elif lows[t] <= target:
                    xp = target
                else:
                    xp = None
            if xp is not None:
                cost_e = _fill_cost(variety, dates, entry_i, entry_price, tick)
                cost_x = _fill_cost(variety, dates, t, xp, tick)
                gross = (xp - entry_price) if side == "L" else (entry_price - xp)
                net = gross - cost_e - cost_x
                trades.append(dict(
                    entry_date=str(pd.Timestamp(dates[entry_i]).date()),
                    exit_date=str(pd.Timestamp(dates[t]).date()),
                    side=side, entry=entry_price, exit=xp,
                    gross=gross, cost=cost_e + cost_x, net=net,
                    holding_bars=t - entry_i, reason=entry_reason,
                    exit_kind=("stop" if xp == stop else "target")))
                realized += net
                pos = 0
                side = None

        # ---- B. 累加 bar(t-1) 到触达图（历史推进到 t-1）----
        i = t - 1
        if valid[i]:
            add_range(touches, e0, bw, lows[i], highs[i])
            add_box(exp, e0, bw, closes[i], (highs[i] - lows[i]) / 2.0)
            if highs[i] > ath:
                ath = float(highs[i]); ath_idx = i
            if lows[i] < atl:
                atl = float(lows[i]); atl_idx = i

        # ---- C. t 日决策（用 ≤ t-1 的滚动状态）----
        if t >= warmup:
            if z_cache is None or (t - last_z_t) >= z_every:
                z = z_score(touches, exp)
                levels, zlev = strong_levels(
                    centers, z, touches, cfg.support_z_thr, cfg.support_min_touches)
                z_cache, levels_cache, zlev_cache = z, levels, zlev
                last_z_t = t
            else:
                z, levels, zlev = z_cache, levels_cache, zlev_cache

            mu = float(mu_series[t - 1]) if (t - 1) < len(mu_series) else 0.0
            sigma = float(sigma_series[t - 1]) if (t - 1) < len(sigma_series) else 0.0

            ctx = dict(
                price=closes[t], low_t=lows[t], high_t=highs[t], tick=tick,
                atr=float(atr[t]), mu=mu, sigma=sigma, t=t,
                strong_levels=levels, strong_z=zlev,
                ath_price=ath, ath_day=ath_idx, atl_price=atl, atl_day=atl_idx,
                s3_peak_arr=s3_peak_arr, s3_trough_arr=s3_trough_arr,
                holding_side=(side if pos != 0 else None),
                s2_lookback_bars=s2_lb, s3_window_bars=s3_w, cfg=cfg)
            sig = generate_signal(ctx)
            if pos == 0 and sig["side"] in ("L", "S"):
                pending_side = sig["side"]
                pending_stop = sig["stop_dist"]
                pending_target = sig["target_dist"]
                pending_reason = sig["reason"]
            elif pos != 0 and sig["exit"]:
                exit_pending = True

        # ---- D. 盯市权益 ----
        if pos != 0:
            if side == "L":
                eq_points[t] = realized + (closes[t] - entry_price)
            else:
                eq_points[t] = realized + (entry_price - closes[t])
        else:
            eq_points[t] = realized

    return _aggregate(variety, trades, eq_points, multiplier, centers, touches, exp,
                      edges, e0, closes, highs, lows,
                      initial_capital_yuan=cfg.initial_capital_yuan)


def _empty_result(variety):
    return dict(variety=variety, n_trades=0, win_rate=0.0, pf=0.0,
                total_net_points=0.0, total_net_yuan=0.0, max_dd=0.0,
                sharpe=0.0, avg_hold=0.0, trades=[],
                level_stats=None, summary={})


def _aggregate(variety, trades, eq_points, multiplier, centers, touches, exp,
               edges, e0, closes, highs, lows, initial_capital_yuan=1_000_000.0):
    df = pd.DataFrame(trades)
    n = len(df)
    if n == 0:
        agg = dict(n_trades=0, win_rate=0.0, pf=0.0, total_net_points=0.0,
                   total_net_yuan=0.0, max_dd=0.0, sharpe=0.0, avg_hold=0.0)
    else:
        wins = df[df.net > 0]
        losses = df[df.net < 0]
        gross_w = wins.net.sum()
        gross_l = -losses.net.sum()
        pf = float(gross_w / gross_l) if gross_l > 0 else float("inf")
        # 点数权益挂到标准化本金上换算人民币权益，再用峰谷回撤（避免除零爆炸）
        equity = initial_capital_yuan + eq_points * multiplier
        peak = np.maximum.accumulate(equity)
        with np.errstate(divide="ignore", invalid="ignore"):
            dd = np.where(peak > 0, (equity - peak) / peak, 0.0)
        valid = dd[np.isfinite(dd)]
        max_dd = float(valid.min()) if len(valid) else 0.0
        d_eq = np.diff(equity)
        sd = d_eq.std()
        sharpe = float(np.mean(d_eq) / sd * np.sqrt(252)) if sd > 0 else 0.0
        agg = dict(
            n_trades=n,
            win_rate=float(len(wins) / n),
            pf=pf,
            total_net_points=float(df.net.sum()),
            total_net_yuan=float(df.net.sum() * multiplier),
            max_dd=max_dd,
            sharpe=sharpe,
            avg_hold=float(df.holding_bars.mean()),
        )
    # 点位触达描述统计（数据基础）
    from .engine import singleton_levels, support_zones, topology_summary
    singletons = singleton_levels(centers, touches, 1)
    doubles = singleton_levels(centers, touches, 2)
    z = z_score(touches, exp)
    zones = support_zones(centers, z, touches, 2.0, 8)[:30]
    ath_price = float(highs.max())
    atl_price = float(lows.min())
    topo = topology_summary(closes)
    summary = dict(
        ath_price=ath_price, atl_price=atl_price, n_bins=len(centers),
        n_singletons=len(singletons), n_double=len(doubles),
        top_support=zones, topology=topo,
        median_daily_range=float(np.median(highs - lows)))
    level_stats = pd.DataFrame({
        "price": centers,
        "touches": touches.astype(int),
        "exp_touch": exp,
        "z": z,
    })
    return dict(variety=variety, **agg, trades=trades,
                level_stats=level_stats, summary=summary)
