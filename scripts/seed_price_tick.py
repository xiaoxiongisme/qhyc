# -*- coding: utf-8 -*-
"""P0-1 辅助：从行情数据派生 futures_symbol.price_tick（最小变动价位）。

tick 估计 = 该品种 888 连续序列中相邻收盘价的最小正价差（模态）。
属**数据派生、非编造**；仅更新 888 连续码，真实合约下单时由 continuous_fallback 复用同品种 tick。

用法（云端容器内，避免本地写云库）：
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/seed_price_tick.py            # dry-run 打印
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/seed_price_tick.py --apply    # 落库
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from app.core.db import session_scope


def _modal_tick(session, table, tcol, pcol, where, params):
    """取某表某标的相邻收盘价的最小正价差（模态 tick）。"""
    rows = session.execute(text(
        f"SELECT {pcol} FROM {table} WHERE {where} ORDER BY {tcol}"), params).fetchall()
    prices = [float(r[0]) for r in rows if r[0] is not None]
    if len(prices) < 2:
        return None
    diffs = sorted({round(prices[i + 1] - prices[i], 6)
                    for i in range(len(prices) - 1) if prices[i + 1] - prices[i] > 0})
    return diffs[0] if diffs else None


def derive_tick(session, symbol: str):
    # 优先 hourly_bar（高精度）
    tick = _modal_tick(session, "hourly_bar", "trade_datetime", "close",
                       "symbol=:s", {"s": symbol})
    if tick:
        return tick
    # 回退：contract_daily（已全量回补，覆盖全部品种）。取该品种任一合约日线收盘的
    # 模态价差作为 tick（同品种各合约 tick 一致；rollover 跳空是大正值，不影响最小正值）。
    product = symbol[:-3] if symbol.endswith("888") else symbol
    return _modal_tick(session, "contract_daily", "trade_date", "close",
                       "symbol LIKE :p", {"p": f"{product}%"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落库；否则仅 dry-run 打印")
    args = ap.parse_args()
    with session_scope() as s:
        syms = [r[0] for r in s.execute(text(
            "SELECT symbol FROM futures_symbol WHERE symbol LIKE '%888'")).fetchall()]
        updates = []
        for sym in syms:
            tick = derive_tick(s, sym)
            if tick:
                updates.append((sym, tick))
                print(f"{sym}: tick≈{tick}")
        if args.apply:
            for sym, tick in updates:
                s.execute(text("UPDATE futures_symbol SET price_tick=:t WHERE symbol=:s"),
                          {"t": tick, "s": sym})
            print(f"已写 {len(updates)} 行 price_tick")
        else:
            print(f"[dry-run] 将写 {len(updates)} 行；--apply 才落库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
