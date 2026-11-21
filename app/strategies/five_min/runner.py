# -*- coding: utf-8 -*-
"""5m 策略框架 · 扫描编排（P1，CL-20261121-04）。

只产出**意图**（intent），不直接下单：
  1. 先过 P0A 的 bar_5m 逐品种新鲜度/完备性门（不过直接拒绝）；
  2. barstore 读 raw continuous 5m + 日线（barstore 默认 back_adj，这里显式 raw）；
  3. 指标 + 两族事件信号 + 单持仓因果重放；
  4. 按 exit_guard 口径生成离场计划（1R≙3ATR / TP=2.5R / tp1 禁用）；
  5. 若声明 pool_id 且 P0C 开关打开，再过池级单持仓闸。

下单由调用方显式执行（走 `/execution/reverse` 反解真实合约 + `/execution/orders`
落单），本模块永远不绕过 EXECUTION_ENABLED 门禁。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import numpy as np
from sqlalchemy import text

from app.data.bar5m_gate import require_fresh
from app.strategies.five_min.engine import ExitPlan, lot_size
from app.strategies.five_min.indicators import DEFAULT_CFG, attach_indicators
from app.strategies.five_min.signals import scan_signals

DEFAULT_SYMBOLS = ("EB888", "SR888", "FG888", "JD888", "MA888",
                   "M888", "UR888", "PP888", "A888", "BU888")


def load_contract_meta(session, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """从 futures_symbol + dim_trading_cost 读取 multiplier/tick/fee（只读）。"""
    rows = session.execute(
        text("SELECT symbol, multiplier, price_tick FROM public.futures_symbol "
             "WHERE symbol = ANY(:syms)"),
        {"syms": list(symbols)},
    ).mappings().all()
    meta: dict[str, dict[str, Any]] = {
        str(r["symbol"]): {"mult": float(r["multiplier"] or 0),
                           "tick": float(r["price_tick"] or 0),
                           "fee": 3.0}
        for r in rows
    }
    # 手续费按品种取：固定元/边；比例品种按最近价折算成元/边（CLOSE/OPEN 同侧口径）。
    for sym in meta:
        variety = "".join(ch for ch in sym if ch.isalpha())
        fee_row = session.execute(
            text("SELECT fee_type, fee_value FROM public.dim_trading_cost "
                 "WHERE variety_code = :v AND action = 'OPEN' "
                 "AND effective_from <= CURRENT_DATE "
                 "AND (effective_to IS NULL OR effective_to >= CURRENT_DATE) "
                 "ORDER BY effective_from DESC, id DESC LIMIT 1"),
            {"v": variety},
        ).mappings().first()
        if fee_row is None:
            continue
        if str(fee_row["fee_type"]).upper() == "FIXED":
            meta[sym]["fee"] = float(fee_row["fee_value"])
        else:
            meta[sym]["fee_pct_per_10000"] = float(fee_row["fee_value"])
    return meta


def _fee_per_side(m: dict[str, Any], last_px: float) -> float:
    if "fee_pct_per_10000" in m:
        return float(m["fee_pct_per_10000"]) / 10000.0 * last_px * float(m["mult"])
    return float(m.get("fee", 3.0))


def scan_once(
    session,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    *,
    now: datetime | None = None,
    cfg: dict[str, Any] | None = None,
    pool_id: str | None = None,
    max_age_min: int | None = 90,
    lookback_bars: int = 1200,
    meta: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """产出意图列表；每个意图含离场计划与 R500 手数，status ∈ READY/BLOCKED。"""
    from app.data import barstore
    from app.strategies.five_min.pool_position import PoolPositionGuard

    p = dict(DEFAULT_CFG)
    if cfg:
        p.update(cfg)
    syms = list(symbols)
    mm: dict[str, datetime] = {}
    bm: dict[str, datetime] = {}
    # 新鲜度门用轻量 SQL，不先加载 1200 根 bar（fail-fast）。
    from app.data.bar5m_gate import load_bar5m_max, load_minute_max

    mm = load_minute_max(session, syms)
    bm = load_bar5m_max(session, syms)
    require_fresh(syms, mm, bm, now=now, max_age_min=max_age_min)

    if meta is None:
        meta = load_contract_meta(session, syms)
    guard = PoolPositionGuard() if pool_id else None
    out: list[dict[str, Any]] = []
    for sym in syms:
        m = meta.get(sym)
        if not m or not m.get("mult") or not m.get("tick"):
            continue
        bars = barstore.load(sym, freq="5m", caliber="continuous", limit=lookback_bars)
        daily = barstore.load(sym, freq="daily", caliber="continuous", limit=400)
        if bars.empty or "ts" not in bars:
            continue
        bars = bars.rename(columns={"ts": "dt"})
        if "ts" in daily:
            daily = daily.rename(columns={"ts": "dt"})
        ctx = attach_indicators(bars, daily, p)
        sigs = scan_signals(ctx, p)
        if not sigs:
            continue
        s = sigs[-1]
        a = float(ctx["atr"].to_numpy(float)[s.index])
        if not np.isfinite(a) or a <= 0:
            continue
        lots = lot_size(float(p["risk_r"]), a, float(m["mult"]), float(p["sl_atr"]))
        if lots < 1:
            continue
        entry_px = float(ctx["close"].to_numpy(float)[s.index])
        plan = ExitPlan(entry_px, s.side, a, float(p["sl_atr"]), float(p["tp_atr"]), lots)
        intent = {
            "status": "READY", "symbol_continuous": sym, "signal": s.as_dict(),
            "price_type": "LIMIT", "lots": lots, "risk_r": float(p["risk_r"]),
            "atr": a, "entry_px": entry_px, "exit_plan": plan.to_exit_guard_payload(),
            "fee_per_side": _fee_per_side(m, entry_px),
            "price_space": "raw", "pool_id": pool_id,
        }
        if guard is not None:
            ok, reason = guard.check_open(pool_id, sym, str(s.dt))
            if not ok:
                intent.update(status="BLOCKED", reason=reason)
        out.append(intent)
    return out


def to_order_intent(intent: dict[str, Any], real_symbol: str, exchange: str) -> dict[str, Any]:
    """转成 `/execution/orders` 的请求体（调用方仍需经 reverse 校验真实合约）。"""
    if intent.get("status") != "READY":
        raise ValueError(f"[fail-loud] 非 READY 意图不可下单：{intent.get('status')}")
    side = intent["signal"]["side"]
    return {
        "real_symbol": real_symbol,
        "exchange": exchange,
        "action": "OPEN",
        "direction": "BUY" if side > 0 else "SELL",
        "price": float(intent["entry_px"]),
        "price_type": "LIMIT",
        "price_space": "raw",
        "lots": int(intent["lots"]),
        "symbol_continuous": intent["symbol_continuous"],
        "signal_id": f"5M:{intent['symbol_continuous']}:{intent['signal']['dt']}",
        "pool_id": intent.get("pool_id"),
    }
