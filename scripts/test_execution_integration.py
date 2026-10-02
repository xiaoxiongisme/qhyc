# -*- coding: utf-8 -*-
"""P0-1 全面集成测试（云端容器内运行，连云端权威库；非本地写库）。

覆盖：
  A. 端到端反解 resolve_signal_to_order（真实库）：选合约/反解/手数/校验全链路
  B. price_space=raw 与 =adj 两条分支（验证偏移逻辑）
  C. HTTP 路由冒烟（localhost:8000，含鉴权状态）
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from datetime import date

from sqlalchemy import text

from app.core.db import session_scope
from app.execution.schemas import ReverseRequest
from app.execution.service import resolve_signal_to_order

SYMBOLS = ["RB888", "AG888", "AU888", "I888", "FG888", "MA888",
           "SA888", "TA888", "BU888", "HC888"]


def latest_close(s, sym):
    r = s.execute(text(
        "SELECT close FROM hourly_bar WHERE symbol=:s "
        "ORDER BY trade_datetime DESC LIMIT 1"), {"s": sym}).fetchone()
    return float(r[0]) if r else None


def http_smoke():
    out = {}
    try:
        with urllib.request.urlopen("http://localhost:8000/openapi.json", timeout=5) as r:
            spec = json.load(r)
            out["openapi_status"] = r.status
            out["execution_paths"] = [p for p in spec.get("paths", {}) if p.startswith("/execution")]
    except Exception as e:  # noqa: BLE001
        out["openapi_error"] = repr(e)
    body = json.dumps({
        "symbol": "RB888", "trade_date": "2026-10-01", "direction": "LONG",
        "entry_px": 3000.0, "target_risk": 20000.0, "atr": 60.0,
        "price_space": "raw",
    }).encode()
    req = urllib.request.Request(
        "http://localhost:8000/execution/reverse", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            out["post_noauth_status"] = r.status
    except urllib.error.HTTPError as e:
        out["post_noauth_status"] = e.code
    except Exception as e:  # noqa: BLE001
        out["post_noauth_error"] = repr(e)
    return out


def resolve(s, sym, td, space):
    close = latest_close(s, sym)
    if close is None:
        return None, "SKIP: 无 hourly_bar 数据"
    req = ReverseRequest(
        symbol=sym, trade_date=td, direction="LONG",
        entry_px=float(close), target_risk=20000.0,
        atr=round(close * 0.02, 2), price_space=space)
    return req, None


def main():
    L = []
    log = lambda x="": L.append(str(x))  # noqa: E731

    with session_scope() as s:
        td_row = s.execute(text("SELECT MAX(trade_date) FROM main_contract_map")).fetchone()
        td = td_row[0] if td_row and td_row[0] else date.today()
        log(f"=== P0-1 全面集成测试 (trade_date={td}) ===")
        log("")
        log("## A. 端到端反解（price_space=raw，真实库全链路）")
        ok = blocked = skip = 0
        for sym in SYMBOLS:
            try:
                req, skipmsg = resolve(s, sym, td, "raw")
                if req is None:
                    log(f"[{sym}] {skipmsg}"); skip += 1; continue
                o = resolve_signal_to_order(s, req)
                if o.blocking_reasons:
                    blocked += 1; status = "BLOCK"
                else:
                    ok += 1; status = "OK"
                log(f"[{sym}] {status} | real={o.real_symbol} exch={o.exchange} "
                    f"mult={o.multiplier} px={o.price} lots={o.lots} notional={o.notional}")
                if o.warnings:
                    log(f"        WARN: {o.warnings}")
                if o.blocking_reasons:
                    log(f"        BLOCK: {o.blocking_reasons}")
            except Exception as e:  # noqa: BLE001
                log(f"[{sym}] EXCEPTION: {type(e).__name__}: {e}")
        log(f"小结: 可下单={ok}  阻断={blocked}  跳过={skip}")
        log("")

        log("## B. 价格空间分支对比（RB888：raw vs adj，验证偏移逻辑）")
        for space in ("raw", "adj"):
            req, skipmsg = resolve(s, "RB888", td, space)
            if req is None:
                log(f"  RB888/{space}: {skipmsg}"); continue
            o = resolve_signal_to_order(s, req)
            log(f"  RB888/{space}: real_px={o.price} cum_offset(meta)={o.meta.get('cum_offset')} "
                f"lots={o.lots} blocking={o.blocking_reasons}")
        log("")

        log("## C. HTTP 路由冒烟（localhost:8000）")
        for k, v in http_smoke().items():
            log(f"  {k}: {v}")

    print("\n".join(L))


if __name__ == "__main__":
    main()
