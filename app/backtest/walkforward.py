# -*- coding: utf-8 -*-
"""Walk-forward 走前回测（PRD §8.5 / 整改优先级 P1）。

为什么单独成模块
----------------
`walk_fusion_states` 天然无前视（每根 bar 只用 ≤ i 的数据），但那只保证**信号层**
不偷看未来；PRD §8.5 还要求：滚动窗口切分、**到期用滚动窗重估参数**、报告
**OOS 衰减比（OOS 净利 / IS 净利）**。这三件事此前没有落地（测试报告 §2/§8 记为 ❌）。

防前视口径（强约束）
-------------------
1. 参数估计只用**训练窗**数据（`tr_start..tr_end`），绝不触碰测试窗；
2. 测试窗信号需要热身（至少 `min_bars` 根 bar），热身数据取**测试窗起点之前**的真实
   历史（合法），但最终只保留 `entry_dt >= te_start` 的成交进入 OOS 统计；
3. 窗口按时间**单向前移**，任何窗都看不到自己的未来窗。

判定（PRD §8.5）：
- 衰减比 ≥ 50% → 维持 V3.4 定稿口径；
- 深度衰减（< 50% 或 OOS 净利为负）→ 建议回退 P0 口径（fib_confl:true + add_max_lots:1）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.core.logging import logger
from app.backtest.fusion_backtest import (
    FusionBacktestParams,
    fusion_params_from_config,
    run_fusion_backtest,
)


@dataclass
class WalkForwardParams:
    """Walk-forward 窗口与重估参数。"""
    train_days: int = 720        # 训练窗（日历天）
    test_days: int = 60          # 测试窗 / OOS 窗（日历天）
    step_days: int = 60          # 滚动步长（日历天）
    anchored: bool = False       # True = 锚定式（训练窗起点固定、评估窗前移）
    warmup_days: int = 150       # 测试窗之前的热身历史（保证 min_bars 起算，非前视）
    reestimate: bool = False     # 是否在训练窗上重估参数（True 时成本 ×网格数）
    reestimate_grid: list[dict] = field(default_factory=list)
    min_oos_trades: int = 5      # 单窗最少 OOS 成交，不足则该窗跳过（样本不支持结论）
    decay_pass: float = 0.50     # OOS/IS ≥ 该值判定为「未深度衰减」
    max_positions: int = 1


# 默认重估网格（reestimate=True 且未显式指定时使用）：围绕 V3.4 定稿做小幅扰动
_DEFAULT_GRID: list[dict] = [
    {},                      # 基线 = V3.4 定稿
    {"sl_atr": 1.5},
    {"sl_atr": 2.5},
]


def _net(trades: list[dict]) -> float:
    """已平仓成交的净盈亏（价格单位；与 pnl 口径一致，用于相对比较与衰减比）。"""
    return float(sum(t["pnl"] for t in trades if not t.get("open")))


def _count_closed(trades: list[dict]) -> int:
    return sum(1 for t in trades if not t.get("open"))


def _windows(start: date, end: date, p: WalkForwardParams):
    """生成 (tr_start, tr_end, te_start, te_end) 窗口序列，时间单向前移。"""
    trd, ted, st = p.train_days, p.test_days, max(1, p.step_days)
    if p.anchored:
        te_start = start + timedelta(days=trd)
        while te_start < end:
            te_end = min(te_start + timedelta(days=ted), end)
            yield start, min(te_start - timedelta(days=1), end), te_start, te_end
            te_start = te_start + timedelta(days=st)
    else:
        cur = start
        w = trd + ted
        while cur + timedelta(days=w) <= end:
            te_start = cur + timedelta(days=trd)
            te_end = cur + timedelta(days=w) - timedelta(days=1)
            yield cur, te_start - timedelta(days=1), te_start, te_end
            cur = cur + timedelta(days=st)


def _apply(base: FusionBacktestParams, overrides: dict) -> FusionBacktestParams:
    q = FusionBacktestParams(**{k: getattr(base, k)
                                for k in base.__dataclass_fields__})
    for k, v in (overrides or {}).items():
        if hasattr(q, k):
            setattr(q, k, v)
    return q


def _dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


def run_walk_forward(session, symbols: list[str] | None = None,
                     base: FusionBacktestParams | None = None,
                     params: WalkForwardParams | None = None,
                     start: date | None = None, end: date | None = None,
                     verbose: bool = True) -> dict:
    """滚动 walk-forward：逐窗「训练窗选参（可选）→ 测试窗出 OOS」。

    Returns:
        dict(windows=[...], n_oos_trades, is_net, oos_net, decay,
             verdict, oos_trades=[...], params={...})
        —— `oos_trades` 可直接喂 `robustness.to_robustness_trades` 做六项检查。
    """
    p = params or WalkForwardParams()
    base = base or fusion_params_from_config()
    if symbols is None:
        from app.core.config import get_settings
        symbols = [s.symbol for s in get_settings().main_contracts]

    if start is None or end is None:
        start, end = _data_range(session, symbols, base.src)
    if start is None or end is None:
        return {"windows": [], "n_oos_trades": 0, "is_net": 0.0, "oos_net": 0.0,
                "decay": None, "verdict": "无数据", "oos_trades": [],
                "params": vars(p)}

    grid = p.reestimate_grid or _DEFAULT_GRID
    windows: list[dict] = []
    all_oos: list[dict] = []
    is_net_total = 0.0
    oos_net_total = 0.0

    for tr_s, tr_e, te_s, te_e in _windows(start, end, p):
        chosen, chosen_over, is_net = base, {}, float("nan")
        if p.reestimate:
            best = None
            for over in grid:
                cand = _apply(base, over)
                n = _net(_flatten(run_fusion_backtest(
                    session, symbols, cand, tr_s, tr_e, include_trades=True)))
                if best is None or n > best[0]:
                    best = (n, cand, over)
            if best:
                is_net, chosen, chosen_over = best
        # —— 测试窗：热身区 + 测试窗，但只统计 te_start 之后的成交 ——
        run_from = te_s - timedelta(days=p.warmup_days)
        res = run_fusion_backtest(session, symbols, chosen, run_from, te_e,
                                  include_trades=True)
        trades = _flatten(res)
        oos = [t for t in trades
               if not t.get("open") and (d := _dt(t.get("entry_dt"))) is not None
               and d.date() >= te_s]
        n_oos = len(oos)
        if n_oos < p.min_oos_trades:
            if verbose:
                logger.info(f"[wf] {te_s}~{te_e} 跳过：OOS 成交 {n_oos} < {p.min_oos_trades}")
            continue
        oos_net = _net(oos)
        if not p.reestimate:
            # IS 用同一套参数在训练窗上得到（保证 OOS/IS 可比）
            rr = run_fusion_backtest(session, symbols, chosen, tr_s, tr_e,
                                     include_trades=True)
            is_net = _net(_flatten(rr))
        all_oos.extend(oos)
        oos_net_total += oos_net
        is_net_total += is_net if math.isfinite(is_net) else 0.0
        rec = {
            "train": [str(tr_s), str(tr_e)], "test": [str(te_s), str(te_e)],
            "n_oos": n_oos, "is_net": round(is_net if math.isfinite(is_net) else 0.0, 4),
            "oos_net": round(oos_net, 4),
            "chosen": chosen_over or "baseline",
        }
        if math.isfinite(is_net) and abs(is_net) > 1e-12:
            rec["decay"] = round(oos_net / is_net, 4)
        windows.append(rec)
        if verbose:
            logger.info(f"[wf] {te_s}~{te_e} OOS={oos_net:.4f} (n={n_oos}) "
                        f"IS={is_net if math.isfinite(is_net) else float('nan'):.4f}")

    decay = None
    if abs(is_net_total) > 1e-12:
        decay = round(oos_net_total / is_net_total, 4)
    verdict = _verdict(decay, oos_net_total, p)
    return {
        "windows": windows, "n_windows": len(windows),
        "is_net": round(is_net_total, 4), "oos_net": round(oos_net_total, 4),
        "decay": decay, "verdict": verdict,
        "n_oos_trades": len(all_oos), "oos_trades": all_oos,
        "symbols": symbols, "range": [str(start), str(end)],
        "params": {**vars(p), "base": {k: getattr(base, k)
                                       for k in base.__dataclass_fields__}},
    }


def _verdict(decay: float | None, oos_net: float, p: WalkForwardParams) -> str:
    if decay is None:
        return "样本不足：无法判定（需 ≥1 个有效窗且 IS 净利非零）"
    if oos_net <= 0:
        return ("❌ OOS 净利为负 —— 深度衰减：建议回退 P0 口径 "
                "(fib_confl=true + add_max_lots=1) 并重跑")
    if decay >= p.decay_pass:
        return f"✅ 衰减比 {decay:.2%} ≥ {p.decay_pass:.0%} —— 维持 V3.4 定稿口径"
    return (f"⚠ 衰减比 {decay:.2%} < {p.decay_pass:.0%} —— 深度衰减："
            "建议回退 P0 口径 (fib_confl=true + add_max_lots=1)")


def _flatten(res: dict) -> list[dict]:
    """把 run_fusion_backtest 的返回摊平成成交列表。

    兼容两种形态：新版可能直接带 `oos_trades` / `trades`，旧版只有 per_symbol
    聚合（无逐笔），此时返回空列表并由上层判 「样本不足」。
    """
    for k in ("trades", "oos_trades"):
        v = res.get(k)
        if isinstance(v, list) and v:
            return v
    agg = res.get("trades")
    return agg if isinstance(agg, list) else []


def _net_from_result(res: dict) -> float:
    return _net(_flatten(res))


def _data_range(session, symbols: list[str], src: str):
    """取符号集合在 hourly_bar 上的最大公共可用区间（start/end）。"""
    from sqlalchemy import select, func as f
    from app.models import HourlyBar
    if not symbols:
        return None, None
    row = session.execute(
        select(f.min(HourlyBar.trade_datetime), f.max(HourlyBar.trade_datetime))
        .where(HourlyBar.symbol.in_(symbols)).where(HourlyBar.src == src)
    ).first()
    if not row or row[0] is None:
        return None, None
    mn, mx = row
    return mn.date(), mx.date()
