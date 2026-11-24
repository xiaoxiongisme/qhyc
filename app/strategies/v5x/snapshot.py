# -*- coding: utf-8 -*-
"""V5X 理论持仓快照（只读，不写任何表）— CL-20261109-08。

要解决的问题
------------
V5X 是**无状态回放**扫描器：它每轮只推「当根 bar 发生了什么事件」，
从不告诉用户「按这套规则，现在理论上应该持有什么」。V3.4 停用后，
原先由盘前预播报/定时简报承担的那个作用整体消失了。

后果在 2026-11-09 第一次真正实盘前暴露得很直接：按 11-07 末根重放，
V5X 理论上持有 5 个多头（A888 / P888 / SF888 / SS888 / Y888），
而用户手上一个都没有。于是：
  · 之后推这 5 个品种的 ``CLOSE``，用户无仓可平；
  · 只有新 ``OPEN`` 才跟得进去；
  · 统计口径与实盘口径从此永久错位，而且**没有任何地方会提醒**。

本模块就是那个提醒：把引擎内部 ``state / lots / avg_px / e_atr`` 读出来，
按真实可成交价（BA 价反解）输出一份清单。

三条纪律
--------
1. **只读**：不写 ``execution_order`` / ``execution_position`` / ``fusion_position``，
   也不写 ``v5x_signal_log``。与已关闭的持仓同步（CL-20261108-26）无关。
2. **同一份重放实现**：直接复用 :func:`app.strategies.v5x.live._replay_orders`，
   不另写一套循环 —— 两套重放迟早会分叉，那种分歧最难查。
3. **价格反解到真实价**：BA 空间可为负（JD888 实测 -462），绝不能直接给用户看。

用法
----
    python -m app.strategies.v5x.snapshot              # 打印
    python -m app.strategies.v5x.snapshot --json       # 机器可读
    python -m app.strategies.v5x.snapshot --push       # 推送到微信（每日 15:05 由调度器调）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy import text  # noqa: E402

from app.core.db import session_scope  # noqa: E402
from app.core.logging import logger  # noqa: E402
from app.strategies.v5x.live import _load, _replay_orders  # noqa: E402
from app.strategies.v5x.params import config_from  # noqa: E402

__all__ = ["theoretical_positions", "format_snapshot", "push_snapshot",
           "latest_real_prices"]


def latest_real_prices(session, symbols: list[str]) -> dict[str, tuple[float, object]]:
    """每个品种最新已落库分钟的**真实价**（``continuous`` 层，无需反解 off）。

    CL-20261124-01：整点简报在采集/60m 聚合完成前触发时，回放末根会退到上一根
    60m 桶（BR888 实测推 14845，而 22:00 夜盘桶已到 14805）。分钟表逐分钟写、
    与 60m raw 同属真实价空间，用它只刷展示层的「现价/浮亏」，比等 60m 桶聚合
    快一根；止损/方向/手数仍由 60m 回放决定，不受影响。

    纪律：
      * **窗口化**：``ts >= now() - interval '5 days'``，禁止对 6000 万行分钟表全扫；
      * **降级不报错**：某品种取不到时返回缺失，调用方回退 60m 口径价；
      * **只读**：不写任何表。
    """
    if not symbols:
        return {}
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (symbol) symbol, close, ts "
            "FROM l0_raw.minute_bar "
            "WHERE symbol = ANY(:syms) AND layer = 'continuous' "
            "  AND ts >= now() - interval '5 days' AND close IS NOT NULL "
            "ORDER BY symbol, ts DESC"
        ),
        {"syms": list(symbols)},
    ).all()
    return {str(sym): (float(close), ts) for sym, close, ts in rows}


def _cur_stop(eng) -> float:
    """当前有效止损（BA 空间），与 ``SymbolEngine.on_bar`` 内的离场判据同式。

    只读复算，不改引擎状态；用于告诉用户「这笔仓现在离止损还有多远」。
    """
    cfg = eng.cfg
    if eng.state == 1:
        st = eng.avg_px - cfg.SL_ATR * eng.e_atr
        t = eng.peak - cfg.TRAIL_ATR * eng.e_atr
        if t > st:
            st = t
        if eng.be_done and eng.entry_px > st:
            st = eng.entry_px
        return float(st)
    if eng.state == 2:
        st = eng.avg_px + cfg.SL_ATR * eng.e_atr
        t = eng.trough + cfg.TRAIL_ATR * eng.e_atr
        if t < st:
            st = t
        if eng.be_done and eng.entry_px < st:
            st = eng.entry_px
        return float(st)
    return float("nan")


def theoretical_positions(session, symbols: list[str] | None = None,
                          asof: date | None = None,
                          overrides: dict | None = None,
                          use_live_price: bool = True) -> dict:
    """重放到最后一根已收盘 bar，返回理论上仍持有的仓位。

    返回 ``{"asof", "last_bar", "rows", "n_long", "n_short", "n_flat", "stale_days"}``。
    每行含 ``symbol/dir/lots/avg_px_real/entry_px_real/last_price/stop_real/
    unrealized_yuan/entry_bar/last_bar``，价格全部为**真实可成交价**。

    CL-20261124-01：``last_price`` / ``unrealized_yuan`` 默认取**最新分钟真实价**
    （展示层，解决整点简报现价滞后一根 60m 桶）；同行的 ``bar_last_price`` 保留
    60m 回放末根价作同源对账。``stop_real`` / ``avg_px_real`` / ``dir`` / ``lots``
    仍由 60m 回放决定，**不随展示价变动**——止损判据与信号口径绝不改。
    设 ``use_live_price=False`` 可回到纯 60m 口径（守卫/回放对拍用）。
    """
    asof = asof or date.today()
    # ★ 2026-11-09（CL-20261109-16）：``load_market_data`` 的区间是 **[start, end) 半开**，
    #   这里原样把 ``asof`` 当 end 传 ⇒ 只取到 asof 当天 00:00 之前 ⇒ **永远落后一整天**。
    #   实测：11-09 15:05 推的快照 ``last_bar = 2026-11-07 01:00``（周五夜盘末），
    #   当天日盘四根完全没参与，用户拿到的是一份过期清单。
    #   ``scan_once`` 传的是 ``(now + 1 day).date()``（见 live.py:288），两处必须一致。
    md, syms = _load(session, symbols, asof + timedelta(days=1))
    if md is None:
        return {"asof": str(asof), "last_bar": None, "rows": [],
                "n_long": 0, "n_short": 0, "n_flat": 0, "stale_days": None}

    cfg = config_from(overrides or {})
    # CL-20261124-01：循环前一次性取全部品种的分钟真实价（避免逐品种 N+1 查询）。
    # 取不到/异常一律降级为空 dict，调用方回退 60m 口径——展示降级，不影响交易判据。
    live: dict[str, tuple[float, object]] = {}
    if use_live_price:
        try:
            live = latest_real_prices(session, list(syms))
        except Exception as exc:                          # noqa: BLE001
            logger.warning("[v5x.snapshot] 分钟实时价不可用，回退 60m 口径: {0}: {1}".format(
                type(exc).__name__, str(exc)[:120]))
    rows: list[dict] = []
    n_flat = 0
    last_bar = None
    for sym in syms:
        df = md.bars.get(sym)
        if df is None or len(df) < 200:
            continue
        holder: dict = {}
        off = 0.0
        ts = None
        for _i, ts, off, _orders in _replay_orders(md, sym, df, cfg, holder):
            pass                        # 只为把状态机推到末根
        eng = holder.get("engine")
        if eng is None:
            continue
        if last_bar is None or ts > last_bar:
            last_bar = ts
        if eng.state == 0:
            n_flat += 1
            continue

        dirn = 1 if eng.state == 1 else -1
        mult = float(eng.cost.mult)
        avg_real = eng.avg_px - off
        entry_real = eng.entry_px - off
        bar_last_real = float(df["close"].iloc[-1]) - off   # 60m 回放末根真实价（对账口径）
        stop_real = _cur_stop(eng) - off
        hit = live.get(sym)
        if hit is not None:
            show_price, price_ts = hit
            price_source = "minute"
            price_ts = str(price_ts)
        else:
            show_price = bar_last_real
            price_ts = None
            price_source = "bar_60m"
        rows.append({
            "symbol": sym,
            "dir": "LONG" if dirn > 0 else "SHORT",
            "lots": round(float(eng.lots), 2),
            "avg_px_real": round(avg_real, 4),
            "entry_px_real": round(entry_real, 4),
            "last_price": round(show_price, 4),          # 展示层：分钟实时价（缺失回退 bar）
            "bar_last_price": round(bar_last_real, 4),   # 60m 回放口径，对账用
            "price_source": price_source,                # 取价来源，供守卫/文案判别
            "price_ts": price_ts,
            "stop_real": round(stop_real, 4),            # 止损：仍由 60m 回放决定，不动
            "unrealized_yuan": round((show_price - avg_real) * dirn * eng.lots * mult, 2),
            "be_done": bool(eng.be_done),
            "entry_bar": str(df["bucket"].iloc[eng.entry_i]),
            "last_bar": str(ts),
            "price_ba": round(float(eng.avg_px), 4),
        })

    rows.sort(key=lambda r: (r["dir"] != "LONG", r["symbol"]))
    stale = None
    if last_bar is not None:
        try:
            stale = (datetime.now(last_bar.tzinfo) - last_bar).days
        except Exception:                                    # noqa: BLE001
            stale = None
    return {
        "asof": str(asof),
        "last_bar": str(last_bar) if last_bar is not None else None,
        "rows": rows,
        "n_long": sum(1 for r in rows if r["dir"] == "LONG"),
        "n_short": sum(1 for r in rows if r["dir"] == "SHORT"),
        "n_flat": n_flat,
        "stale_days": stale,
    }


def format_snapshot(snap: dict, max_rows: int = 20) -> str:
    """把快照渲染成推送文本（可直接发微信）。"""
    rows = snap.get("rows") or []
    head = f"[qhyc V5X] 理论持仓\n截至 {snap.get('last_bar') or '—'}"
    if not rows:
        return (head + "\n空仓（38 白名单中无任何持仓）\n"
                "—— 这是「按规则现在应该持有什么」的答案，不是你的实盘持仓")

    lines = [head,
             f"共 {len(rows)} 个：多 {snap.get('n_long', 0)} / 空 {snap.get('n_short', 0)}"]
    # CL-20261124-01：现价改取分钟实时价后，明确标注取价来源/时点，
    # 让用户不再把「现」误读为 60m 收盘价，也说明止损仍按 60m 桶。
    minute_rows = [r for r in rows if r.get("price_source") == "minute"]
    if minute_rows:
        pts = sorted({r.get("price_ts") for r in minute_rows if r.get("price_ts")})
        ts_note = f"截至 {pts[-1][11:16]}" if pts else ""
        lines.append(f"现价取分钟实时价{ts_note}；止损/均价按 60m 桶（回放口径不变）")
    for r in rows[:max_rows]:
        tag = "保本" if r.get("be_done") else "止损"
        lines.append(
            f"{r['symbol']} {'多' if r['dir'] == 'LONG' else '空'} {r['lots']:g}手 "
            f"均价{r['avg_px_real']:.1f} 现{r['last_price']:.1f} "
            f"({r['unrealized_yuan']:+,.0f}) {tag}{r['stop_real']:.1f}")
    if len(rows) > max_rows:
        lines.append(f"…另 {len(rows) - max_rows} 个略")

    stale = snap.get("stale_days")
    if stale is not None and stale >= 2:
        lines.append(f"⚠ 最后一根 bar 已是 {stale} 天前，清单可能过期")
    lines.append("纪律：存量不追，只跟之后的新开仓")
    return "\n".join(lines)


def push_snapshot(session=None, asof: date | None = None) -> dict:
    """计算 + 推送（调度器每日收盘后调用）。推送失败**外抛**，不静默。"""
    from app.core.config import get_settings
    from app.notify import send_notify

    if session is None:
        with session_scope() as s:
            snap = theoretical_positions(s, asof=asof)
    else:
        snap = theoretical_positions(session, asof=asof)
    text = format_snapshot(snap)
    # 备用通道与交易信号共用同一个配置项（CL-20261109-06 的教训：漏传 = 单点）
    fallback = getattr(get_settings().fusion, "fallback_webhook", "") or ""
    ok, via = send_notify("[qhyc V5X] 理论持仓", text, fallback)
    logger.info(f"[v5x] 持仓快照推送 ok={ok} via={via} n={len(snap.get('rows') or [])}")
    if not ok:
        raise RuntimeError(f"V5X 持仓快照推送失败（pushplus + fallback 均未送达，via={via}）")
    return {"ok": bool(ok), "via": via, "n": len(snap.get("rows") or []),
            "last_bar": snap.get("last_bar")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="V5X 理论持仓快照（只读）")
    ap.add_argument("--asof", help="截止日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--push", action="store_true", help="推送到微信")
    a = ap.parse_args(argv)
    asof = date.fromisoformat(a.asof) if a.asof else None
    if a.push:
        rep = push_snapshot(asof=asof)
        print(json.dumps(rep, ensure_ascii=False))
        return 0 if rep.get("ok") else 1
    with session_scope() as s:
        snap = theoretical_positions(s, asof=asof)
    print(json.dumps(snap, ensure_ascii=False, indent=2) if a.json
          else format_snapshot(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
