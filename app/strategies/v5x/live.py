# -*- coding: utf-8 -*-
"""V5X.3 小时级信号扫描（C3-min：只推送，不建单、不回写持仓）。

设计要点
========
1. **无状态回放**：每次扫描重放最近 ``LOOKBACK_DAYS`` 天的 60m bar，取**最后一根
   已收盘 bar** 的订单事件。不持久化 ``SymbolEngine`` 内部状态 —— 于是既没有
   「状态表与真实脱节」的第二份真相，也不会因进程重启/漏跑而漂移。
   代价是每轮重算 ~3 千根 bar × 38 品种，实测可接受。

2. **t-1 门控**（``gate_lag=1`` + ``regime_mode="strict"``）：三道日频门
   （hvol / 簇冲击 / 体制）只用 ≤ T-1 的日线聚合，全天恒定。这是小时级扫描
   唯一可复现的口径；``gate_lag=0`` 的回测口径要等当日收盘，盘中算不出来。

3. **出口价格反解**：V5X 信号活在 BA 后复权空间（``ba = raw + adj_offset``，
   强品种会转负，如 JD888 实测 ``ba.close = -462``）。推送**必须**给真实可成交价：
   ``real = ba - adj_offset``，其中 ``adj_offset = ba_close - raw_c``（取信号那根
   bar 自带的两列相减，无需再查表）。BA 价同时保留在日志里便于对账。

4. **只读**：本模块**不**写 ``execution_order``、**不**写 ``fusion_position``。
   下单与持仓同步是 C3 的第二阶段，未经影子对账不接。

去重
====
``dedupe_key = symbol|bar_ts|action|side``，落在 ``public.v5x_signal_log``
（UNIQUE）。同一根 bar 的同一动作只推一次；重跑扫描不会重复轰炸。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.logging import logger
from app.data.tradable_scope import load_tradable_pool
from app.strategies.v5x.context import assign_td, build_ctx, build_cost, load_market_data
from app.strategies.v5x.engine import SymbolEngine
from app.strategies.v5x.params import config_from

__all__ = [
    "V5XSignal",
    "SCAN_HOURS",
    "scan_once",
    "format_message",
    "run_v5x_scan",
]

#: 回放窗口（自然日）。日频门控要 250 个交易日（≈360 自然日）的振幅/波动，
#: 再加 60m 指标预热（EMA140 ≈ 25 个交易日），540 天足够且留余量。
LOOKBACK_DAYS = 540
#: 单品种最少 bar 数：低于此值视为预热不足，跳过（不产信号）
MIN_BARS = 200
#: 最后一根 bar 的最大年龄（分钟）。超过即视为数据陈旧，**本轮该品种不产信号**。
#: 来源：生产 V3.4 在 2026-09-23 因陈旧 K 线一轮误推 18 条信号，此后加了同类门控。
#: 取 90 分钟：小时线最坏情况是「整点采集延迟 + 扫描在 :05 触发」，正常 ≤ 65 分钟。
MAX_BAR_AGE_MIN = 90
#: 允许扫描的小时（收盘后那根 bar 才有意义）：日盘 09-11/13-15、夜盘 21-23
SCAN_HOURS = frozenset({9, 10, 11, 13, 14, 15, 21, 22, 23})
#: V5X 信号价格源（与回测 v5x_backtest.py 默认一致）
SRC = "bar_60m_ba"
#: 实盘口径：t-1 门控 + 无前视体制
GATE_LAG = 1
REGIME_MODE = "strict"


@dataclass
class V5XSignal:
    """一条待推送信号（价格已反解到真实合约价）。"""

    symbol: str
    action: str                 # OPEN / ADD / CLOSE
    side: str                   # LONG / SHORT
    bar_ts: Any                 # bar 收盘时刻
    adj_offset: float           # BA 与真实价的加性偏移
    price_ba: float
    price_real: float
    lots: float
    stop_ba: float | None = None
    stop_real: float | None = None
    etype: str = ""             # pb=回踩 / bk=突破
    reason: str | None = None   # stop / tp
    net_yuan: float | None = None

    @property
    def dedupe_key(self) -> str:
        return f"{self.symbol}|{self.bar_ts}|{self.action}|{self.side}"


def _td_series(buckets: pd.Series, open_days: np.ndarray) -> np.ndarray:
    """bar 收盘时刻 → 所属交易日（夜盘归次一交易日）。"""
    return assign_td(buckets, open_days)


def scan_once(session, symbols: list[str] | None = None, *,
              now: datetime | None = None,
              overrides: dict | None = None) -> list[V5XSignal]:
    """扫描一轮，返回**最后一根已收盘 bar** 产生的信号（未去重）。

    Args:
        symbols: 品种池；``None`` 时取 ``l3_ref.tradable_variety`` 的 38 个白名单主连。
        now: 判定「已收盘」的基准时刻，默认当前时间。``bucket <= now`` 才参与。
    """
    now = now or datetime.now()
    if symbols is None:
        symbols = [f"{v}888" for v in sorted(load_tradable_pool(session))]
    if not symbols:
        logger.warning("[v5x.live] 品种池为空 → 本轮不扫描")
        return []

    end = (now + timedelta(days=1)).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    md = load_market_data(session, symbols, start, end, src=SRC,
                          regime_mode=REGIME_MODE, gate_lag=GATE_LAG)
    if not md.bars:
        logger.warning(f"[v5x.live] 未取到任何 bar（src={SRC}）→ 本轮不扫描")
        return []

    cfg = config_from(overrides or {})
    sigs: list[V5XSignal] = []
    skipped: list[str] = []

    for sym in symbols:
        df = md.bars.get(sym)
        if df is None or df.empty:
            skipped.append(sym)
            continue
        # 只吃已收盘的 bar：bucket 是收盘时刻，未收盘的那根不进状态机
        df = df[df["bucket"] <= now].reset_index(drop=True)
        if len(df) < MIN_BARS:
            skipped.append(sym)
            continue

        tds = _td_series(df["bucket"], md.open_days)
        offs = (df["close"].astype(float) - df["raw_c"].astype(float)).to_numpy()
        # 新鲜度门：陈旧 K 线一律不产信号（宁可漏推，不可用过期数据误导下单）
        last_ts = df["bucket"].iloc[-1]
        age_min = (now - pd.Timestamp(last_ts).to_pydatetime()).total_seconds() / 60.0
        if age_min > MAX_BAR_AGE_MIN:
            logger.warning(f"[v5x.live] {sym} 最后一根 bar 陈旧 {age_min:.0f} 分钟"
                           f"（>{MAX_BAR_AGE_MIN}）→ 跳过")
            skipped.append(sym)
            continue

        eng = SymbolEngine(sym, cfg, build_cost(sym, df["bucket"].iloc[-1].date()))
        prev_td = None
        orders: list[dict] = []
        for i in range(len(df)):
            td = tds[i]
            if td != prev_td:
                eng.set_daily_ctx(build_ctx(md, sym, td))
                prev_td = td
            r = df.iloc[i]
            orders = eng.on_bar(r["bucket"], float(r["open"]), float(r["high"]),
                                float(r["low"]), float(r["close"]),
                                float(r["volume"]), float(r["raw_c"]))
        if not orders:
            continue

        off = float(offs[-1])
        ts = df["bucket"].iloc[-1]
        for o in orders:
            act = str(o.get("type"))
            d = int(o.get("dir") or 0)
            side = "LONG" if d > 0 else "SHORT"
            p_ba = float(o.get("price"))
            stop_ba = o.get("stop")
            sigs.append(V5XSignal(
                symbol=sym,
                action=act,
                side=side,
                bar_ts=ts,
                adj_offset=off,
                price_ba=p_ba,
                price_real=float(p_ba - off),
                lots=float(o.get("lots") or 1.0),
                stop_ba=float(stop_ba) if stop_ba is not None else None,
                stop_real=float(stop_ba - off) if stop_ba is not None else None,
                etype=str(o.get("etype") or ""),
                reason=o.get("reason"),
                net_yuan=float(o["net"]) if "net" in o else None,
            ))

    logger.info(f"[v5x.live] 扫描完成：{len({s.symbol for s in sigs})} 品种出信号，"
                f"{len(skipped)} 品种跳过（预热不足/无 bar/数据陈旧）")
    return sigs


def _persist(session, sigs: Iterable[V5XSignal]) -> list[V5XSignal]:
    """写信号日志并返回**本次新增**的那些（已推过的自动过滤）。"""
    new: list[V5XSignal] = []
    for s in sigs:
        row = session.execute(text("""
            INSERT INTO public.v5x_signal_log
                (dedupe_key, symbol, bar_ts, action, side, price_ba, adj_offset,
                 price_real, stop_ba, stop_real, lots, etype, reason, net_yuan)
            VALUES (:k, :sym, :ts, :act, :side, :pba, :off, :preal,
                    :sba, :sreal, :lots, :etype, :reason, :net)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
        """), {
            "k": s.dedupe_key, "sym": s.symbol, "ts": s.bar_ts, "act": s.action,
            "side": s.side, "pba": s.price_ba, "off": s.adj_offset,
            "preal": s.price_real, "sba": s.stop_ba, "sreal": s.stop_real,
            "lots": s.lots, "etype": s.etype, "reason": s.reason,
            "net": s.net_yuan,
        }).first()
        if row is not None:
            new.append(s)
    return new


def _mark_notify(session, sigs: Iterable[V5XSignal], ok: bool, via: str) -> None:
    for s in sigs:
        session.execute(text("""
            UPDATE public.v5x_signal_log
               SET notify_ok = :ok, notify_via = :via
             WHERE dedupe_key = :k
        """), {"ok": ok, "via": via, "k": s.dedupe_key})


def format_message(sigs: list[V5XSignal]) -> tuple[str, str]:
    """生成推送标题与正文（价格一律真实合约价，BA 价附在括号里备查）。"""
    n_open = sum(1 for s in sigs if s.action == "OPEN")
    n_close = sum(1 for s in sigs if s.action == "CLOSE")
    n_add = sum(1 for s in sigs if s.action == "ADD")
    title = f"[V5X] {len(sigs)} 条信号（开{n_open}/加{n_add}/平{n_close}）"

    lines: list[str] = []
    icon = {"OPEN": "🚨", "ADD": "➕", "CLOSE": "✅"}
    for s in sorted(sigs, key=lambda x: (x.action != "OPEN", x.symbol)):
        tag = f"{icon.get(s.action, '•')} {s.action} {s.side}"
        lines.append(f"{tag}  {s.symbol} @ {s.price_real:,.2f}"
                     f"（BA {s.price_ba:,.2f}，偏移 {s.adj_offset:+,.2f}）")
        if s.stop_real is not None:
            lines.append(f"   ⛔止损 {s.stop_real:,.2f}")
        if s.action == "CLOSE":
            lines.append(f"   原因 {s.reason or '-'}，净 {s.net_yuan:+,.0f} 元")
        elif s.action == "OPEN":
            lines.append(f"   路径 {'回踩' if s.etype == 'pb' else '突破'}，手数 {s.lots:g}")
        lines.append(f"   bar {s.bar_ts}")
    lines.append("")
    lines.append("口径：t-1 门控（hvol/簇冲击/体制均只用 ≤T-1 日线），BA 后复权信号 + "
                 "真实价反解；同根收盘成交假设，偏乐观。")
    lines.append("⚠️ 只推信号，不建单、不碰持仓；是否下单由你自己判断。")
    return title, "\n".join(lines)


def run_v5x_scan(session, *, now: datetime | None = None,
                 do_push: bool = True, overrides: dict | None = None) -> dict[str, Any]:
    """扫描 + 去重落库 + 推送。返回摘要（供调度器写 task_run / 日志）。"""
    now = now or datetime.now()
    sigs = scan_once(session, now=now, overrides=overrides)
    if not sigs:
        return {"n_signals": 0, "n_new": 0, "pushed": False}

    new = _persist(session, sigs)
    session.commit()
    if not new:
        logger.info(f"[v5x.live] 本轮 {len(sigs)} 条信号均为已推过的历史 bar → 不重复推送")
        return {"n_signals": len(sigs), "n_new": 0, "pushed": False}

    if not do_push:
        return {"n_signals": len(sigs), "n_new": len(new), "pushed": False}

    from app.notify import send_notify
    title, content = format_message(new)
    ok, via = send_notify(title, content)
    _mark_notify(session, new, ok, via)
    session.commit()
    logger.info(f"[v5x.live] 推送 {len(new)} 条（{via}）：ok={ok}")
    return {"n_signals": len(sigs), "n_new": len(new), "pushed": bool(ok), "via": via}
