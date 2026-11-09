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

from app.core.db import session_scope  # noqa: E402
from app.core.logging import logger  # noqa: E402
from app.strategies.v5x.live import _load, _replay_orders  # noqa: E402
from app.strategies.v5x.params import config_from  # noqa: E402

__all__ = ["theoretical_positions", "format_snapshot", "push_snapshot"]


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
                          overrides: dict | None = None) -> dict:
    """重放到最后一根已收盘 bar，返回理论上仍持有的仓位。

    返回 ``{"asof", "last_bar", "rows", "n_long", "n_short", "n_flat", "stale_days"}``。
    每行含 ``symbol/dir/lots/avg_px_real/entry_px_real/last_price/stop_real/
    unrealized_yuan/entry_bar/last_bar``，价格全部为**真实可成交价**。
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
        last_real = float(df["close"].iloc[-1]) - off
        stop_real = _cur_stop(eng) - off
        rows.append({
            "symbol": sym,
            "dir": "LONG" if dirn > 0 else "SHORT",
            "lots": round(float(eng.lots), 2),
            "avg_px_real": round(avg_real, 4),
            "entry_px_real": round(entry_real, 4),
            "last_price": round(last_real, 4),
            "stop_real": round(stop_real, 4),
            "unrealized_yuan": round((last_real - avg_real) * dirn * eng.lots * mult, 2),
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
