# -*- coding: utf-8 -*-
"""V5X.3 回测器（qhyc 数据 / 真实费率）— CL-20261108-22 / C2。

与 ``fusion_backtest`` 的关系
----------------------------
两套**独立策略**，共用同一份 K 线与费率，不共状态机：

| | ``fusion_backtest``（V3.4，生产） | 本模块（V5X.3，研究） |
|---|---|---|
| 信号 | ``walk_fusion_states`` 生成器，逐帧 state | ``SymbolEngine.on_bar`` 事件驱动 orders |
| 成交口径 | E1-C：下一根开盘 ±1tick + 0.5ATR 跳空过滤 | **同根收盘**（V5X 冻结口径） |
| 门控 | ADX / 斐波 / 加码 | 高波日 / 化工只回踩 / 尾盘根 / MACD 同号 / 会员 abs(z) |
| 参数源 | ``FusionConfig``（42 字段） | ``V5XConfig``（20 项冻结） |

★ 为什么本模块默认 ``fill_mode="close"``
  这是 V5X 冻结版的原始口径，也是唯一能与 ``trades46_V5X.csv`` / 历史消融数字
  （+244,742 等）对拍的路径。改成 next_open 需要「信号用 c、成交用 O[i+1]」的
  双价重放（引擎内部 ``avg_px`` / ``_exit(fill=c)`` 都要重算），属 C2b 范围，
  本次**未实现**，因此本模块的净盈亏**偏乐观**，不得直接与 E1-C 口径的
  ``fusion_backtest`` 数字并列引用。

已知实证（38 白名单 / 12 年 / qhyc 真实费率 / close 口径）
----------------------------------------------------------
· 无门控 -423,264 → +M1A -65,096 → +M1A+M5 +95,493（原始价）
· +M1A+M5 / 后复权 +244,742，但 88% 来自 2022，近两年连续亏损
· ``hvol`` 单独打开 -819,634（严重有害）；``REGIME_P2_AUTO`` 在 qhyc 上未验证
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.core.logging import logger
from app.data.tradable_scope import load_tradable_pool
from app.strategies.v5x.context import (
    V5XMarketData,
    assign_td,
    build_cost,
    build_ctx,
    load_market_data,
)
from app.strategies.v5x.engine import SymbolEngine
from app.strategies.v5x.params import config_from, describe

__all__ = ["V5XBacktestParams", "run_v5x_backtest"]


@dataclass
class V5XBacktestParams:
    """回测参数（策略参数走 ``overrides``，其余是回测口径）。"""

    src: str = "bar_60m_ba"        # V5X 冻结版用后复权；"bar_60m" 复现原始价对照
    regime_mode: str = "frozen"    # frozen=复现 V5X（含前视）/ strict=无前视
    overrides: dict[str, Any] = field(default_factory=dict)
    # —— 三道「上下文门」的开关（对应 V5X 的 ctx.hvol / ctx.blocked / ctx.regime）——
    # 默认全开 = 逐位复现 V5X.3。消融时关掉即可，不必改引擎。
    # ⚠ 实测（38 池 / 12 年）：hvol 单独打开净利 -819,634（严重有害），
    #   关掉它做对照是必要的研究动作，但**不得**因此改默认值 —— 默认值代表
    #   「冻结版是什么」，改了就再也复现不出历史数字。
    use_hvol: bool = True
    use_blocked: bool = True
    use_regime: bool = True
    # P6（会话末根禁开仓）。V5X.3 引擎自带，但历史消融脚本 v5x_on_qhyc.py 传的是
    # 空集合（未启用）。要与那批数字（-423,264 / +95,493 / +244,742）逐位对拍，
    # 必须显式置 False —— 否则笔数会因「尾盘根不开仓」而偏少，差异不可归因。
    use_p6: bool = True
    # V5X 冻结回测假设每笔 1 手、单品种串行；本模块同口径，不做资金约束。
    note: str = ""


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"n_trades": 0, "net_yuan": 0.0, "win_rate": None,
                "avg_r": None, "profit_factor": None}
    net = np.array([float(t["net"]) for t in trades], dtype=float)
    rs = np.array([float(t["r"]) for t in trades], dtype=float)
    rs = rs[np.isfinite(rs)]
    pos = float(net[net > 0].sum())
    neg = float(-net[net < 0].sum())
    return {
        "n_trades": int(len(trades)),
        "net_yuan": round(float(net.sum()), 2),
        "gross_yuan": round(float(sum(float(t["gross"]) for t in trades)), 2),
        "win_rate": round(float((net > 0).mean()), 4),
        "avg_yuan": round(float(net.mean()), 2),
        "avg_r": round(float(rs.mean()), 4) if len(rs) else None,
        "profit_factor": round(pos / neg, 4) if neg > 0 else None,
    }


def run_v5x_backtest(session, symbols: list[str] | None = None,
                     start: date | None = None, end: date | None = None,
                     params: V5XBacktestParams | None = None,
                     include_trades: bool = False) -> dict:
    """跑 V5X.3 回测（单品种串行、每笔 1 手、同根收盘成交）。

    ★ 品种池（CL-20261108-12）：默认取 ``l3_ref.tradable_variety`` 的 38 个白名单
      主连，与 ``fusion_backtest`` 同一口径；显式传入的 symbols 不会被悄悄收窄，
      但也不会被放行到非白名单（由调用方负责）。
    """
    p = params or V5XBacktestParams()
    cfg = config_from(p.overrides)
    if symbols is None:
        pool = load_tradable_pool(session)
        symbols = [f"{v}888" for v in sorted(pool)]
    start = start or date(2015, 1, 1)
    end = end or date.today()

    md: V5XMarketData = load_market_data(
        session, symbols, start, end, src=p.src, regime_mode=p.regime_mode)

    all_trades: list[dict] = []
    per_symbol: list[dict] = []
    skipped: list[str] = []
    for sym in symbols:
        df = md.bars.get(sym)
        if df is None or len(df) < 200:
            skipped.append(sym)
            continue
        eng = SymbolEngine(sym, cfg, build_cost(sym, start))
        tds = assign_td(df["bucket"], md.open_days)
        prev_td = None
        for row, td in zip(df.itertuples(index=False), tds):
            if td != prev_td:
                ctx = build_ctx(md, sym, td)
                if not p.use_hvol:
                    ctx.hvol = False
                if not p.use_blocked:
                    ctx.blocked = False
                if not p.use_regime:
                    ctx.regime = None
                    ctx.vol_z = None
                if not p.use_p6:
                    ctx.sess_last_hours = frozenset()
                eng.set_daily_ctx(ctx)
                prev_td = td
            eng.on_bar(row.bucket, float(row.open), float(row.high),
                       float(row.low), float(row.close), float(row.volume),
                       float(row.raw_c), allow=True)
        tr = eng.trades
        if not tr:
            skipped.append(sym)
            continue
        for t in tr:
            t["symbol"] = sym
        all_trades.extend(tr)
        m = _metrics(tr)
        m["symbol"] = sym
        per_symbol.append(m)

    agg = _metrics(all_trades)
    agg["per_symbol"] = per_symbol
    agg["skipped"] = skipped
    agg["params"] = describe(cfg)
    agg["overrides"] = dict(p.overrides)
    agg["price_space"] = {
        "src": p.src,
        "signal": "后复权（去换月跳空）" if "ba" in p.src else "原始主连价",
        "fill": "同根收盘（V5X 冻结口径，偏乐观；E1-C next_open 未实现）",
    }
    agg["regime_mode"] = p.regime_mode
    agg["gates"] = {"hvol": p.use_hvol, "blocked": p.use_blocked,
                    "regime": p.use_regime}
    agg["diagnostics"] = md.diagnostics

    # 分年（V5X 的核心结论是「2022 依赖」，必须每次都能看到）
    yearly: dict[str, dict] = {}
    for t in all_trades:
        xdt = t.get("xdt")
        y = str(getattr(xdt, "year", str(xdt)[:4]))
        a = yearly.setdefault(y, {"n": 0, "net": 0.0})
        a["n"] += 1
        a["net"] += float(t["net"])
    agg["yearly"] = {k: {"n": v["n"], "net": round(v["net"], 2)}
                     for k, v in sorted(yearly.items())}

    if include_trades:
        agg["trades"] = all_trades
    if skipped:
        logger.info(f"[v5x] {len(skipped)} 个品种无成交/数据不足：{skipped[:8]}…")
    return agg
