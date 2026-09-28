# -*- coding: utf-8 -*-
"""A · 隐秘数轴（magic axis / 魔力线）策略 —— 生产版纯函数。

来源：2026-09-27 从《复盘》文本提炼，原实现在 `runtime/bt_engine_min15.py:run_magic`。
2026-09-29 迁入 `app/strategies/`（六层解耦 · 策略层），使其与其它代码一样
进 git、可部署、可被回测层直接调用。

机制（日内区间均值回归）
------------------------
前一日的最高/最低（PDH/PDL）构成一根"数轴"。当日价格触及数轴端点后，
若出现**反向吞没**确认，则反向入场：
    做多：l[r] <= pdl 且 c[s] > o[s] 且 c[e] > h[s]
    做空：h[r] >= pdh 且 c[s] < o[s] 且 c[e] < l[s]
止损 = 数轴外侧极值 ± 0.2×ATR；目标 = 对侧极值；
窗口 = 入场后 240 根，窗口内未触及则按最后一根收盘强制离场
（`MAGIC_EOD`）—— 这一步是**修幸存者偏差**的关键，不可省略。

与融合策略的关系
----------------
融合 V3.4 是**顺势趋势跟踪**，A 是**逆势区间回归**，二者本质对立。
2026-09-26 实测并联会稀释收益（净¥ 18.09 万 → 10.04 万）。
故 A **独立于融合并跑**，不叠加进融合状态机（架构铁律：不改 walk_fusion_states）。

适用周期：仅在 15m 成立（小时线 PF 1.18 却净亏、信号泛滥）。
"""

from __future__ import annotations

import numpy as np

#: 默认参数（与 2026-09-27 实测口径一致）
DEFAULT_PARAMS = dict(
    atr_n=14,          # ATR 窗口（Wilder 平滑）
    stop_atr=0.2,      # 止损在极值外的 ATR 倍数
    lookback_days=10,  # 极值回溯天数（取 min/max）
    window_bars=240,   # 持仓窗口（根）
)


def atr_wilder(o, h, l, c, n=14):
    """Wilder ATR（与 run_magic 原实现逐位一致）。"""
    m = len(c)
    tr = np.empty(m)
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    atr = np.full(m, np.nan)
    if m < n:
        return atr
    atr[n - 1] = np.nanmean(tr[:n])
    for i in range(n, m):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def run_magic(sym, df_or_arrays, mult=1.0, sub=None, params=None):
    """跑 A 策略，返回 trades 列表（dict 结构同融合回测，便于共用盈亏层）。

    Args:
        sym: 品种代码（仅写入 trade.sym 便于归因）
        df_or_arrays: dict/DataFrame，需含 o,h,l,c,dt 五列
        mult: 合约乘数（用于换算金额；纯 R 口径可留 1.0）
        sub: (a,b) 区间切片（用于走前分段）
        params: 覆盖 DEFAULT_PARAMS

    Returns:
        list[dict]，字段：sym/ei/xi/edt/xdt/dir/ep/xp/atr_e/risk/R/mult/exit_kind
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    if hasattr(df_or_arrays, "columns"):      # DataFrame
        g = df_or_arrays
        o = np.asarray(g["o"], float); h = np.asarray(g["h"], float)
        l = np.asarray(g["l"], float); c = np.asarray(g["c"], float)
        dts = np.asarray(g["dt"])
    else:                                      # dict of arrays
        g = df_or_arrays
        o = np.asarray(g["o"], float); h = np.asarray(g["h"], float)
        l = np.asarray(g["l"], float); c = np.asarray(g["c"], float)
        dts = np.asarray(g["dt"])

    if sub is not None:
        a, b = sub
        o, h, l, c, dts = o[a:b], h[a:b], l[a:b], c[a:b], dts[a:b]
    n = len(c)
    if n < 400:
        return []

    atr = atr_wilder(o, h, l, c, p["atr_n"])
    dates = dts.astype("datetime64[D]")
    uniq, inv = np.unique(dates, return_inverse=True)
    dmax = np.full(len(uniq), -np.inf)
    dmin = np.full(len(uniq), np.inf)
    np.maximum.at(dmax, inv, h)
    np.minimum.at(dmin, inv, l)

    trades = []
    i = 0
    while i < n - 3:
        k = inv[i]
        if k == 0:
            i += 1
            continue
        pdh, pdl = dmax[k - 1], dmin[k - 1]
        day_end = i
        while day_end < n and inv[day_end] == k:
            day_end += 1
        done = False
        for x in range(i, day_end - 2):
            r, s, e = x, x + 1, x + 2
            if l[r] <= pdl and c[s] > o[s] and c[e] > h[s]:
                entry = c[e]
                stop = min(pdl, dmin[max(0, k - p["lookback_days"]):k].min()) \
                       - p["stop_atr"] * atr[e]
                dd = 1
            elif h[r] >= pdh and c[s] < o[s] and c[e] < l[s]:
                entry = c[e]
                stop = max(pdh, dmax[max(0, k - p["lookback_days"]):k].max()) \
                       + p["stop_atr"] * atr[e]
                dd = -1
            else:
                continue
            risk = abs(entry - stop)
            if risk <= 0 or not np.isfinite(atr[e]) or atr[e] <= 0:
                continue
            target = pdh if dd == 1 else pdl
            last_m = min(n - 1, e + p["window_bars"])
            for m in range(e + 1, last_m + 1):
                hit_stop = (l[m] <= stop) if dd == 1 else (h[m] >= stop)
                hit_tgt = (h[m] >= target) if dd == 1 else (l[m] <= target)
                if hit_stop or hit_tgt:
                    # 同时触及 → 保守按止损
                    xp = target if (hit_tgt and not hit_stop) else stop
                    trades.append(dict(sym=sym, ei=e, xi=m, edt=dts[e], xdt=dts[m],
                                       dir=dd, ep=entry, xp=xp, atr_e=atr[e], risk=risk,
                                       R=(xp - entry) * dd / risk, mult=mult,
                                       exit_kind="MAGIC"))
                    done = True
                    break
            if not done:
                # 窗口内未触及 → 强制末根收盘离场（修幸存者偏差，不可省）
                m = last_m
                xp = c[m]
                if np.isfinite(xp) and risk > 0:
                    trades.append(dict(sym=sym, ei=e, xi=m, edt=dts[e], xdt=dts[m],
                                       dir=dd, ep=entry, xp=xp, atr_e=atr[e], risk=risk,
                                       R=(xp - entry) * dd / risk, mult=mult,
                                       exit_kind="MAGIC_EOD"))
                    done = True
            if done:
                break
        i = (trades[-1]["xi"] + 1) if done else day_end
    return trades
