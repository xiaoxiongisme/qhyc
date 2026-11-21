# -*- coding: utf-8 -*-
"""5m 执行引擎（缺口2/3/4）：限价成交 + ATR 离场 + 全局单持仓（CL-20261121-04）。

口径与研究基线 `strategy_engine.run` 完全一致：
* 信号 bar 收盘确认；``next_open``=次根开盘成交（基线），``limit``=信号收盘价挂单、
  次根高低点触及才成交，否则撤单（不成交不记交易）；
* 止损/止盈按**收盘价**判定（过滤盘中插针），成交当根不判定；
* ``lots = floor(R / (sl_atr * ATR * mult))``，<1 手直接跳过；
* 往返成本 = ``2 * (fee + slip_ticks * tick * mult) * lots``（tick 是价格跳值，
  每跳价值 = tick×mult；2026-11-21 P0 修复过的漏乘 mult bug 不再重犯）；
* 在场期间忽略一切新信号，平仓后下一根才开始接单。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

from app.strategies.five_min.indicators import DEFAULT_CFG
from app.strategies.five_min.signals import Signal


@dataclass(frozen=True)
class ExitPlan:
    """qhyc ``exit_guard`` 口径对齐层：1R ≙ 止损距离 = 3ATR，止盈 = 2.5R。

    ⚠ 不要再把 ``init_stop`` 传成 entry±1ATR：那样 r_unit 会变成 1ATR，
    手数与止盈全错（CL-20261121-04 风险登记）。
    """

    entry_px: float
    side: int
    atr: float
    sl_atr: float = 3.0
    tp_atr: float = 7.5
    lot_size: int = 0

    @property
    def r_unit(self) -> float:
        return self.sl_atr * self.atr

    @property
    def stop_px(self) -> float:
        return self.entry_px - self.side * self.r_unit

    @property
    def target_px(self) -> float:
        return self.entry_px + self.side * self.tp_atr * self.atr

    @property
    def tp_r(self) -> float:
        """7.5ATR / 3ATR = 2.5R（非 exit_guard 默认 tp1_r=1.0）。"""
        return self.tp_atr / self.sl_atr

    def to_exit_guard_payload(self) -> dict[str, Any]:
        """交给 exit_guard 的最小口径映射；tp1 明确禁用，避免 3ATR 处砍仓 1/3。"""
        return {
            "init_stop": round(self.stop_px, 6),
            "r_unit": round(self.r_unit, 6),
            "tp1_r": None,          # 禁用分批止盈（研究口径是单一 7.5ATR 目标）
            "tp2_r": round(self.tp_r, 6),
            "long": self.side > 0,
        }


def lot_size(risk_r: float, atr: float, mult: float, sl_atr: float) -> int:
    if not np.isfinite(atr) or atr <= 0 or mult <= 0:
        return 0
    return int(np.floor(risk_r / (sl_atr * atr * mult)))


def run_replay(
    ctx: pd.DataFrame,
    signals: Sequence[Signal],
    symbol: str,
    *,
    mult: float,
    tick: float,
    fee_per_side: float,
    cfg: dict[str, Any] | None = None,
    fill_mode: str = "next_open",
    slip_ticks: float = 1.0,
) -> list[dict[str, Any]]:
    """单持仓因果重放，返回逐笔交易 dict（可直接落研究 CSV 口径）。"""
    p = dict(DEFAULT_CFG)
    if cfg:
        p.update(cfg)
    if fill_mode not in ("next_open", "limit"):
        raise ValueError(f"fill_mode 仅 next_open/limit，收到 {fill_mode!r}")
    if len(ctx) == 0:
        return []
    o = ctx["open"].to_numpy(float)
    h = ctx["high"].to_numpy(float)
    l = ctx["low"].to_numpy(float)
    c = ctx["close"].to_numpy(float)
    atr = ctx["atr"].to_numpy(float)
    dts = list(ctx["dt"]) if "dt" in ctx else [None] * len(ctx)
    sl, tp = float(p["sl_atr"]), float(p["tp_atr"])
    max_bars = p.get("max_bars")
    if max_bars is not None:
        max_bars = int(max_bars)
    events: dict[int, Signal] = {}
    for s in signals:
        events.setdefault(s.index, s)

    trades: list[dict[str, Any]] = []
    i = 0
    n = len(ctx)
    while i < n:
        sig = events.get(i)
        if sig is not None and np.isfinite(atr[i]) and atr[i] > 0 and i + 1 < n:
            a = float(atr[i])
            lots = lot_size(float(p["risk_r"]), a, float(mult), sl)
            if lots >= 1:
                sig_px = float(c[i])
                fill_px, fill_i = None, None
                if fill_mode == "next_open":
                    fill_px, fill_i = float(o[i + 1]), i + 1
                else:
                    j = i + 1
                    if l[j] <= sig_px <= h[j]:
                        fill_px, fill_i = sig_px, j
                if fill_px is not None:
                    plan = ExitPlan(fill_px, sig.side, a, sl, tp, lots)
                    k, exit_i, exit_px, reason = fill_i + 1, None, None, None
                    while k < n:
                        if sig.side > 0:
                            hit_sl, hit_tp = c[k] <= plan.stop_px, c[k] >= plan.target_px
                        else:
                            hit_sl, hit_tp = c[k] >= plan.stop_px, c[k] <= plan.target_px
                        if hit_sl:
                            exit_i, exit_px, reason = k, float(c[k]), "SL"
                            break
                        if hit_tp:
                            exit_i, exit_px, reason = k, float(c[k]), "TP"
                            break
                        if max_bars is not None and (k - fill_i + 1 >= max_bars):
                            exit_i, exit_px, reason = k, float(c[k]), "TIME"
                            break
                        k += 1
                    if exit_i is None:
                        exit_i, exit_px, reason = n - 1, float(c[n - 1]), "EOD"
                    gross = sig.side * (exit_px - fill_px) * float(mult) * lots
                    cost = 2.0 * (fee_per_side + slip_ticks * tick * mult) * lots
                    trades.append({
                        "symbol": symbol, "entry_dt": str(dts[fill_i]),
                        "exit_dt": str(dts[exit_i]), "side": "L" if sig.side > 0 else "S",
                        "entry_px": fill_px, "exit_px": exit_px, "atr": a, "lots": lots,
                        "bars": int(exit_i - fill_i), "reason": reason,
                        "gross": float(gross), "cost": float(cost),
                        "pnl": float(gross - cost), "hist_atr": sig.hist_atr,
                        "family": sig.family, "exit_plan": plan.to_exit_guard_payload(),
                    })
                    i = exit_i + 1
                    continue
        i += 1
    return trades


def summarize(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "total": 0.0, "win": 0.0, "pf": 0.0}
    pnl = np.array([t["pnl"] for t in trades], dtype=float)
    pos, neg = pnl[pnl > 0], pnl[pnl < 0]
    pf = float(pos.sum() / -neg.sum()) if len(neg) and neg.sum() < 0 else 99.0
    return {"n": len(pnl), "total": float(pnl.sum()),
            "win": float((pnl > 0).mean()), "pf": pf}
