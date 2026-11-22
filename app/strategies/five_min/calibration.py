# -*- coding: utf-8 -*-
"""P3 模拟盘校准（CL-20261122-01）：量化"限价触及即成交"的乐观假设。

背景
----
研究基线（strategy_engine / evidence_harden）的限价成交口径是：
    信号 bar 收盘价挂单 → **次根 high/low 触及即成交，且成交价=信号价**。
这是乐观上界：真实盘口里限价单可能排队不成交（成交率<100%），
触及时开盘可能已跳空，"成交价=信号价"也会高估成交质量。

本模块不改任何生产行为，纯离线只读地把这条链拆成可观测的校准量：
  * ``touch_rate``    : 次根触及限价的比例（相对真实入场机会，与 run_replay 同源）；
  * ``gap_ticks``     : 次根开盘相对信号价的跳空（对己方不利为正），按 tick 归一；
  * ``fill_rate_stress``: 随机保留部分信号后的 OOS 净利/PF 退化曲线。

执行逻辑全部复用 :mod:`engine` / :mod:`signals` / :mod:`indicators`，
保证"校准用的执行链"与"生产 runner 的执行链"同源，不另起炉灶。
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd

from app.strategies.five_min.engine import ExitPlan, lot_size, run_replay, summarize
from app.strategies.five_min.indicators import DEFAULT_CFG
from app.strategies.five_min.signals import Signal


def _entry_events(ctx: pd.DataFrame, signals: Sequence[Signal], *, mult: float,
                  cfg: dict[str, Any] | None) -> list[tuple[int, Signal, float, float]]:
    """复刻 run_replay 的单持仓因果，返回真正成为入场机会的事件。

    每项 ``(signal_index, signal, atr, sig_px)``。在场期间的新信号被吞掉，
    与 :func:`run_replay` 事件选择逐位一致——否则撤单率分母会被虚增。
    """
    p = dict(DEFAULT_CFG)
    if cfg:
        p.update(cfg)
    n = len(ctx)
    if n == 0 or mult <= 0:
        return []
    o = ctx["open"].to_numpy(float)
    h = ctx["high"].to_numpy(float)
    l = ctx["low"].to_numpy(float)
    c = ctx["close"].to_numpy(float)
    atr = ctx["atr"].to_numpy(float)
    sl, tp = float(p["sl_atr"]), float(p["tp_atr"])
    max_bars = p.get("max_bars")
    events: dict[int, Signal] = {}
    for s in signals:
        events.setdefault(s.index, s)

    out: list[tuple[int, Signal, float, float]] = []
    i = 0
    while i < n:
        sig = events.get(i)
        if sig is not None and np.isfinite(atr[i]) and atr[i] > 0 and i + 1 < n:
            a = float(atr[i])
            lots = lot_size(float(p["risk_r"]), a, mult, sl)
            if lots >= 1:
                sig_px = float(c[i])
                out.append((i, sig, a, sig_px))
                j = i + 1
                touched = l[j] <= sig_px <= h[j]
                if touched:
                    plan = ExitPlan(sig_px, sig.side, a, sl, tp, lots)
                    k, exit_i = j + 1, None
                    while k < n:
                        if sig.side > 0:
                            bad = c[k] <= plan.stop_px
                            good = c[k] >= plan.target_px
                        else:
                            bad = c[k] >= plan.stop_px
                            good = c[k] <= plan.target_px
                        if bad or good:
                            exit_i = k
                            break
                        if max_bars is not None and (k - j + 1 >= int(max_bars)):
                            exit_i = k
                            break
                        k += 1
                    i = (exit_i if exit_i is not None else n - 1) + 1
                    continue
        i += 1
    return out


def calibrate_touch(
    ctx: pd.DataFrame,
    signals: Sequence[Signal],
    *,
    mult: float,
    tick: float,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统计限价挂单次根触及率 + 开盘跳空分布（tick 归一）。

    ``gap_ticks`` 定义：次根开盘相对信号价，**对己方不利为正**——
      买入(side=+1)：open 高于信号价 ⇒ 需等回落，gap>0（不利）；
      卖出(side=-1)：open 低于信号价 ⇒ 需等反弹，gap>0（不利）。
    """
    entries = _entry_events(ctx, signals, mult=mult, cfg=cfg)
    n = len(ctx)
    if not entries or n == 0:
        return {"attempts": 0, "touched": 0, "expired": 0, "touch_rate": 0.0,
                "gap_ticks_mean": 0.0, "gap_ticks_p90": 0.0}
    o = ctx["open"].to_numpy(float)
    h = ctx["high"].to_numpy(float)
    l = ctx["low"].to_numpy(float)

    touched = expired = 0
    gaps: list[float] = []
    for i, sig, _a, sig_px in entries:
        j = i + 1
        if l[j] <= sig_px <= h[j]:
            touched += 1
            gap = (o[j] - sig_px) * sig.side  # 不利方向为正
            gaps.append(gap / tick if tick > 0 else gap)
        else:
            expired += 1
    return {
        "attempts": len(entries),
        "touched": touched,
        "expired": expired,
        "touch_rate": round(touched / len(entries), 4),
        "gap_ticks_mean": round(float(np.mean(gaps)), 3) if gaps else 0.0,
        "gap_ticks_p90": round(float(np.percentile(gaps, 90)), 3) if gaps else 0.0,
    }


def fill_rate_stress_replay(
    ctx: pd.DataFrame,
    signals: Sequence[Signal],
    symbol: str,
    *,
    mult: float,
    tick: float,
    fee_per_side: float,
    keep_rates: Sequence[float] = (1.0, 0.8, 0.6, 0.4),
    n_rep: int = 20,
    seed: int = 42,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """成交率压力：随机保留比例信号后回放，看 OOS 净利/PF 退化。

    与本地研究 ``evidence_harden.fill_rate_stress`` 同口径（随机丢弃 +
    单持仓重放），但执行逻辑走生产 ``engine.run_replay``，确保生产/研究同源。
    """
    rng = np.random.default_rng(seed + (sum(ord(ch) for ch in symbol) % 1000))
    out: dict[str, Any] = {}
    for kr in keep_rates:
        if kr >= 1.0:
            tr = run_replay(ctx, list(signals), symbol, mult=mult, tick=tick,
                            fee_per_side=fee_per_side, cfg=cfg, fill_mode="limit")
            out[f"keep_{int(round(kr*100))}"] = summarize(tr)
        else:
            reps = []
            for _ in range(n_rep):
                kept = [sig for sig in signals if rng.random() < kr]
                reps.append(summarize(run_replay(
                    ctx, kept, symbol, mult=mult, tick=tick,
                    fee_per_side=fee_per_side, cfg=cfg, fill_mode="limit")))
            out[f"keep_{int(round(kr*100))}"] = {
                "n": int(np.median([r["n"] for r in reps])),
                "total": round(float(np.median([r["total"] for r in reps])), 1),
                "pf": round(float(np.median([r["pf"] for r in reps])), 3),
            }
    return out
