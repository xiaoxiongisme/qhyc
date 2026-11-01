# -*- coding: utf-8 -*-
"""从 akshare 权威近期标签派生每个主连品种的 60m/30m 时段网格。

- 60m 日盘边界：直接用已入库 hourly_bar(src=akshare) 近期（最近 ~150 天）的日盘标签
  → 分钟偏移（相对 09:00）。akshare 新浪 60m 已是同花顺会话切片，权威正确。
- 30m 日盘边界：现场拉 akshare futures_zh_minute_sina(period="30") 取近期日盘标签。
- 夜盘边界：60m/30m 均从 21:00 起按 step 递增，末尾截断到该品种夜盘自然收盘
  （夜盘长度由 60m 夜盘标签最大值推定：23:00→120min / 01:00→240min / 02:30→330min）。

产出 grids.json：{ symbol: { day60:[...], day30:[...], night_end_min:int|null } }
night_end_min = 夜盘末根相对 21:00 的分钟数（无夜盘为 null）。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope

SH = ZoneInfo("Asia/Shanghai")

# 标准回退（多数品种）
STD_DAY60 = [60, 135, 315, 360]          # 10:00/11:15/14:15/15:00
STD_DAY30 = [30, 60, 105, 135, 285, 315, 360]


def _mins_from_0900(h, m):
    return h * 60 + m - 540  # 09:00 = 0


def _mins_from_2100(h, m):
    if h >= 21:
        return (h - 21) * 60 + m
    return (h + 3) * 60 + m  # 0/1/2 点 → 跨午夜


def derive_from_db(symbol: str, days: int = 150) -> dict:
    """从 hourly_bar(akshare) 近期标签派生 60m 日盘边界 + 夜盘末位置。"""
    since = datetime.now(tz=timezone.utc).astimezone(SH) - timedelta(days=days)
    day = []
    night_max = None
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT EXTRACT(hour FROM trade_datetime AT TIME ZONE 'Asia/Shanghai')::int AS h,
                   EXTRACT(minute FROM trade_datetime AT TIME ZONE 'Asia/Shanghai')::int AS m
            FROM l0_raw.hourly_bar
            WHERE symbol=:sym AND src='akshare' AND trade_datetime >= :since
        """), {"sym": symbol, "since": since}).all()
    for h, m in rows:
        if 9 <= h <= 15:
            day.append(_mins_from_0900(h, m))
        elif h >= 21 or h <= 2:
            nm = _mins_from_2100(h, m)
            if night_max is None or nm > night_max:
                night_max = nm
    day = sorted(set(day))
    # 过滤掉明显异常（如 0 或 >360）
    day = [d for d in day if 0 < d <= 360]
    return {"day60": day or None, "night_end_min": night_max}


def fetch_akshare_30m(product: str) -> list[tuple[int, int]]:
    import akshare as ak
    sina = f"{product.lower()}0"
    df = ak.futures_zh_minute_sina(symbol=sina, period="30")
    out = []
    for _, r in df.iterrows():
        ds = str(r.get("datetime"))
        try:
            dt = datetime.strptime(ds, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        out.append((dt.hour, dt.minute))
    return out


def main():
    specs = get_settings().main_contracts
    grids: dict[str, dict] = {}
    for spec in specs:
        sym = spec.symbol
        d = derive_from_db(sym)
        day60 = d["day60"] or STD_DAY60
        night_end = d["night_end_min"]
        # 30m 日盘边界：先尝试 akshare period=30
        day30 = None
        try:
            labels = fetch_akshare_30m(spec.product)
            dl = sorted({_mins_from_0900(h, m) for h, m in labels if 9 <= h <= 15})
            dl = [x for x in dl if 0 < x <= 360]
            if dl:
                day30 = dl
        except Exception as e:
            print(f"  [warn] {sym} 30m 拉取失败: {str(e)[:80]}", file=sys.stderr)
        if day30 is None:
            day30 = STD_DAY30
        grids[sym] = {"day60": day60, "day30": day30, "night_end_min": night_end}
        print(f"  {sym:<8} day60={day60} day30={day30} night_end={night_end}")
    with open("scripts/validate/grids.json", "w", encoding="utf-8") as f:
        json.dump(grids, f, ensure_ascii=False, indent=2)
    print(f"\n已写出 grids.json（{len(grids)} 品种）")


if __name__ == "__main__":
    main()
