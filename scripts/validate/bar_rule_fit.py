# -*- coding: utf-8 -*-
"""交易时段（分钟簇）探查 + bar 切分规则拟合。

目的：不再猜规则。用最细粒度的 1 分钟数据，求出每个交易日的「连续分钟簇」
（相邻分钟间隔 > 1 分钟即断开），再套用几种候选切分规则，看哪个能复现用户
给出的同花顺口径（SC 一天 ≥6 根，形态如 10:00 11:15 14:15 15:00 22:00 23:00）。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.db import session_scope

CST = ZoneInfo("Asia/Shanghai")

CANDIDATES = [
    # name, step_min, skip_break(bool), label_end(bool)
    ("R1 自然60分网格+末根不足单独成bar", 60, False, True),
    ("R2 累计60个交易分钟(跳过休市)", 60, True, True),
    ("R3 累计30个交易分钟(跳过休市)", 30, True, True),
    ("R4 自然30分网格", 30, False, True),
]


def _clusters(mins: list[datetime]) -> list[list[datetime]]:
    """相邻间隔 <=1min 视为同一连续簇（交易小节）。"""
    out: list[list[datetime]] = []
    cur: list[datetime] = []
    for m in mins:
        if cur and (m - cur[-1]) > timedelta(minutes=1):
            out.append(cur)
            cur = []
        cur.append(m)
    if cur:
        out.append(cur)
    return out


def split(mins: list[datetime], step: int, skip_break: bool) -> list[tuple[datetime, list[datetime]]]:
    """返回 [(bar_end_minute, [该bar内的分钟...])]"""
    if skip_break:
        bars: list[list[datetime]] = []
        cur: list[datetime] = []
        for m in mins:
            cur.append(m)
            if len(cur) >= step:
                bars.append(cur)
                cur = []
        if cur:
            bars.append(cur)
        return [(b[-1], b) for b in bars]
    # 自然网格：按绝对分钟 = ceil(ts 到 60/30 分网格)
    bars: dict[int, list[datetime]] = {}
    for m in mins:
        minute_of_day = m.hour * 60 + m.minute
        key = ((minute_of_day + step - 1) // step) * step
        bars.setdefault(key, []).append(m)
    out = []
    for key in sorted(bars):
        group = bars[key]
        out.append((group[-1], group))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SC888")
    ap.add_argument("--date", default="2023-06-15")
    args = ap.parse_args()

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT ts, close FROM l0_raw.minute_bar
            WHERE symbol = :s AND ts >= :d0 AND ts < :d1
            ORDER BY ts
        """), {"s": args.symbol,
               "d0": datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc) - timedelta(days=1),
               "d1": datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc) + timedelta(days=2)}
        ).fetchall()
    if not rows:
        print("无分钟数据")
        return 1

    mins = [r[0].astimezone(CST) for r in rows]
    closes = {r[0].astimezone(CST): float(r[1]) for r in rows}
    print(f"{args.symbol} 分钟数 = {len(mins)}  区间 {mins[0]} ~ {mins[-1]} (CST)")

    print("\n=== 分钟簇（交易小节）===")
    for c in _clusters(mins):
        print(f"  {c[0].strftime('%m-%d %H:%M')} ~ {c[-1].strftime('%H:%M')}   "
              f"n={len(c):>3}  n分钟={(c[-1]-c[0]).total_seconds()/60+1:.0f}")

    print("\n=== 候选规则重建结果（标签=该 bar 最后一根分钟的时刻）===")
    target = "10:00 11:15 14:15 15:00 22:00 23:00"
    print(f"  用户给出的同花顺口径参考：{target}")
    for name, step, skip, _ in CANDIDATES:
        res = split(mins, step, skip)
        times = " ".join(t.strftime("%H:%M") for t, _ in res)
        print(f"\n  --- {name} (step={step}) ---  n={len(res)}")
        print(f"      {times}")
        # 打印每根的分钟跨度
        detail = "  ".join(
            f"{g[0].strftime('%H:%M')}-{t.strftime('%H:%M')}({len(g)})"
            for t, g in res)
        print(f"      跨度: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
