# -*- coding: utf-8 -*-
"""V5X 成交层：把引擎事件按 E1-C 口径重新定价 — CL-20261109-05 / C2b。

问题（为什么需要这个模块）
--------------------------
``SymbolEngine`` 是「逐位移植」的冻结引擎，它的每一次成交都用**当根收盘价**
（``entry_px = avg_px = c``、``_exit(fill=c)``）。这个口径与 ``fusion_backtest``
的 E1-C（``next_open``：信号根收盘出信号 → 下一根开盘 ±1tick 成交 + 0.5 ATR
不利跳空过滤）相差一整个成交假设，两套回测因此**不可比**：

    V5X strict_lag1 报 +273,705（同根收盘）vs V3.4 的 E1-C 口径数字，
    差的不是策略，而是「你凭什么认为自己能在收盘价成交」。

为什么不直接改引擎
------------------
引擎的移植纪律写在 ``engine.py`` 顶部：逻辑一字未改，因为全部结论建立在
「与 ``trades46_V5X.csv`` 逐笔对拍一致」之上。直接改 ``avg_px`` / ``_exit``
会让 close 口径的历史数字**再也复现不出来**，也就失去了唯一的回归锚点。

所以本模块采用「引擎只产事件、成交另算」的分层：

    SymbolEngine.on_bar(...)  →  orders = [OPEN/ADD/CLOSE ...]   （信号层，冻结）
                                  ↓  本模块
    reprice_orders(...)       →  trades[]                        （成交层，可切口径）

引擎的**状态机仍然在收盘价上推演**（这是策略定义的一部分：止损判据是
「收盘跌破」），本模块只负责「同样的决策，换一个更保守的成交价」，并据此
重算 ``avg_px``、手数、成本与 R。这样：

* ``fill_mode="close"`` → **逐位复现**引擎自己的 ``trades``（回归锚点，测试保证）；
* ``fill_mode="next_open"`` → 与 ``fusion_backtest`` 同一口径，两份回测第一次可比。

口径细节（与 ``fusion_backtest._simulate_trades`` 对齐）
--------------------------------------------------------
1. 成交价一律走契约层纯函数 ``app.contracts.execution.fill_price``，不另写一份。
   开仓 ``+dirn×tick``、平仓 ``-dirn×tick``；末尾没有下一根时退回当根收盘仍加滑点。
2. 跳空门：``dirn × (open[i+1] − close[i]) > gap_atr_lim × ATR(i)`` → 该笔**拒绝**。
   语义与 E1-C 一致，是「不追已经跑掉的价」，不是「顺向跳空好事」。
3. 滑点只算一次：``next_open`` 下 1tick 已进成交价，成本里的 ``slip_yuan`` 必须
   置 0，否则双重计入（``fusion_backtest`` 踩过同一个坑）。
4. 跳空门拒绝开仓后的处理（本模块唯一需要**策略判断**的地方）：
   引擎并不知道成交被拒，仍会为这笔「幽灵持仓」继续发 ADD / CLOSE。
   处置 = **整个 episode 作废**（其加码与平仓一并丢弃），直到下一个 OPEN 重新武装。
   这与 E1-C 在 fusion 里的「语义 B：被拒后封锁该方向直到信号归零」同源：
   承认「没成交就是没持仓」，不让一个不存在的仓位产生盈亏。
5. ``same_day``（平今/平昨费率）按**成交根**的交易日判定，而不是信号根 ——
   next_open 跨日成交时这正是费率会变的情形。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

import numpy as np

from app.contracts.execution import VALID_FILL_MODES, fill_price

__all__ = ["reprice_orders", "gap_rejected", "DEFAULT_GAP_ATR"]


DEFAULT_GAP_ATR = 0.5     # E1-C 默认：0.5 倍 ATR 的不利跳空即拒绝


def gap_rejected(dirn: float, i: int, opens, closes, atr, gap_lim: float) -> bool:
    """信号根 ``i`` 的下一根开盘是否「不利跳空」超过阈值。

    与 ``fusion_backtest._simulate_trades`` 内 ``_gap_bad`` 同一判据：
    ``adverse = dirn × (open[j] − close[i])``，``adverse / ATR(i) > gap_lim`` 即拒绝。

    * 长仓：下一根**跳空高开** → adverse > 0 → 追高，拒；
    * 短仓：下一根**跳空低开** → adverse > 0 → 追空，拒；
    * 反向跳空（拿到更好的价）不拒。

    ``ATR(i)`` 非有限或 ≤ 0 时退回「至今 ATR 均值」；仍不可用则放行（fail-open，
    与 fusion 一致：没有风险尺度时不做判断，而不是凭猜测拦单）。
    """
    if gap_lim <= 0:
        return False
    j = i + 1
    n = len(closes)
    if j >= n or i < 0:
        return False
    a = float(atr[i]) if i < len(atr) else float("nan")
    if not np.isfinite(a) or a <= 0:
        hist = np.asarray(atr[: min(i + 1, len(atr))], dtype=float)
        hist = hist[np.isfinite(hist)]
        a = float(hist.mean()) if len(hist) else float("nan")
    if not np.isfinite(a) or a <= 0:
        return False
    adverse = float(dirn) * (float(opens[j]) - float(closes[i]))
    return (adverse / a) > gap_lim


def reprice_orders(
    orders: Sequence[dict],
    *,
    opens: Sequence[float],
    closes: Sequence[float],
    raw_closes: Sequence[float],
    tds: Sequence,
    tss: Sequence,
    atr: Sequence[float],
    cost,
    fill_mode: str = "next_open",
    tick: float = 0.0,
    gap_lim: float = DEFAULT_GAP_ATR,
    sl_atr: float = 1.75,
) -> tuple[list[dict], dict]:
    """把引擎事件序列按指定成交口径重新定价。

    Parameters
    ----------
    orders
        引擎返回的事件列表，每项至少含 ``type``(OPEN/ADD/CLOSE)、``dir``(±1)、
        ``lots``、``price``、``bar_i``（信号根下标，由回测器回填）。
    opens, closes, raw_closes
        与 K 线等长的价格序列（``closes`` 是信号空间价，``raw_closes`` 是
        原始主连价，仅用于费率；两者在非复权源下相同）。
    tds, tss
        每根 K 线所属交易日 / 时间戳，用于 ``same_day`` 费率与成交时间。
    atr
        每根 K 线的 ATR 序列，供跳空门与 R 分母使用（须与引擎同一递推）。
    cost
        ``app.strategies.v5x.engine.CostCfg``。
    fill_mode
        ``"close"``（复现引擎原口径）或 ``"next_open"``（E1-C）。
    tick
        next_open 的单边不利滑点（一个最小变动价位）。close 模式下恒按 0 处理。
    gap_lim
        不利跳空阈值（ATR 倍数）。close 模式下自动失效（历史基线零回归）。
    sl_atr
        初始止损的 ATR 倍数，仅用于把净盈亏折成 R。

    Returns
    -------
    (trades, stats)
        ``trades`` 字段与 ``SymbolEngine.trades`` 同名同义（net/gross/r/P/X/
        lots/e_atr/edt/xdt/reason/dir），可直接喂 ``_metrics``。
        ``stats`` 是漏斗计数，便于把「跳空拒单掉了多少」说清楚。
    """
    if fill_mode not in VALID_FILL_MODES:
        raise ValueError(f"未知 fill_mode={fill_mode!r}（只支持 {' / '.join(VALID_FILL_MODES)}）")

    n = len(closes)
    eff_tick = float(tick) if fill_mode == "next_open" else 0.0
    eff_gap = float(gap_lim) if fill_mode == "next_open" else 0.0
    # 滑点已进成交价 → 成本里必须置 0，否则同一笔 1tick 被计两次
    cost = replace(cost, slip_yuan=0.0) if fill_mode == "next_open" else cost
    mult = float(cost.mult)

    trades: list[dict] = []
    stats = dict(n_open=0, n_add=0, n_close=0, n_open_gap_rejected=0,
                 n_add_gap_rejected=0, n_dropped_episode=0, n_orphan=0,
                 n_tail_fallback=0, n_open_at_end=0)

    pos = None        # {dirn, lots, px_sum, entry_px, entry_raw, entry_i, entry_td, atr}
    dropped = False   # 当前 episode 的开仓被跳空门拒绝 → 其加/平一并作废

    for od in orders:
        typ = str(od.get("type", "")).upper()
        dirn = 1.0 if float(od.get("dir", 0)) >= 0 else -1.0
        i = int(od.get("bar_i", -1))
        if i < 0 or i >= n:
            stats["n_orphan"] += 1
            continue

        if typ == "OPEN":
            stats["n_open"] += 1
            if pos is not None:                 # 定义上不该发生：未平先开
                stats["n_orphan"] += 1
                continue
            if gap_rejected(dirn, i, opens, closes, atr, eff_gap):
                stats["n_open_gap_rejected"] += 1
                dropped = True
                continue
            px, j = fill_price(i, dirn, True, closes, opens, n, fill_mode, eff_tick)
            if fill_mode == "next_open" and i + 1 >= n:
                stats["n_tail_fallback"] += 1
            lots = float(od.get("lots", 1.0) or 1.0)
            a = float(atr[i]) if i < len(atr) else 0.0
            pos = dict(dirn=dirn, lots=lots, px_sum=px * lots,
                       entry_px=px, entry_raw=float(raw_closes[j]),
                       entry_i=i, entry_j=j, entry_td=tds[j], atr=a)
            dropped = False
            continue

        if typ == "ADD":
            stats["n_add"] += 1
            if pos is None or dropped:
                stats["n_orphan"] += 1
                continue
            if gap_rejected(dirn, i, opens, closes, atr, eff_gap):
                stats["n_add_gap_rejected"] += 1
                continue
            px, j = fill_price(i, dirn, True, closes, opens, n, fill_mode, eff_tick)
            lots = float(od.get("lots", 1.0) or 1.0)
            pos["px_sum"] += px * lots
            pos["lots"] += lots
            if fill_mode == "next_open" and i + 1 >= n:
                stats["n_tail_fallback"] += 1
            continue

        if typ == "CLOSE":
            stats["n_close"] += 1
            if dropped:                          # 开仓未成交 → 整段作废，无盈亏
                stats["n_dropped_episode"] += 1
                dropped = False
                continue
            if pos is None:
                stats["n_orphan"] += 1
                continue
            px, j = fill_price(i, dirn, False, closes, opens, n, fill_mode, eff_tick)
            if fill_mode == "next_open" and i + 1 >= n:
                stats["n_tail_fallback"] += 1
            avg_px = pos["px_sum"] / pos["lots"]
            lots = pos["lots"]
            gross = (px - avg_px) * lots * mult if dirn > 0 else (avg_px - px) * lots * mult
            same_day = bool(tds[j] == pos["entry_td"])
            c = cost.cost_round(pos["entry_raw"], float(raw_closes[j]), same_day, lots)
            net = gross - c
            risk = float(sl_atr) * float(pos["atr"] or 0.0) * mult
            trades.append(dict(
                sym=od.get("sym"), dir=dirn,
                edt=tss[pos["entry_j"]], xdt=tss[j],
                P=avg_px, X=px, lots=lots, e_atr=float(pos["atr"] or 0.0),
                net=net, gross=gross,
                r=(net / risk) if risk > 0 else float("nan"),
                reason=str(od.get("reason", "stop")),
                # P0 = 首仓成交价（P 是含加码的均价）；测试与审计要靠它区分
                # 「成交价对不对」和「加码摊平后均价对不对」
                P0=pos["entry_px"],
                signal_i=pos["entry_i"], fill_i=pos["entry_j"], exit_i=j,
            ))
            pos = None
            continue

        stats["n_orphan"] += 1

    if pos is not None and not dropped:
        # 末尾仍持仓：与引擎一致，不强平（回测只统计已平仓交易），但要留痕
        stats["n_open_at_end"] = 1
    return trades, stats
