# -*- coding: utf-8 -*-
"""P0-1 组件①：价格反解（连续价 → 真实合约原始价）。

公式（adj 空间）：real_px = adj_px - cum_offset(t)
复用 app.data.back_adjust：
  - unadjust_price(adj_px, cum_offset)  # 纯数学
  - load_segments(session, symbol, freq) + map_offset(ts, segs)  # as-of 取偏移

实测（2026-10-02 探针）：hourly_bar.<SYM888>.close 是**原始连续价**（未叠加 cum_offset）。
例如 RB888 收盘 3112、cum_offset=2400；若 3112 已后复权则 real=712（螺纹不可能），
故 888 序列为原始连续价 → 默认 price_space="raw"，偏移置 0，real = adj_px。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.data.back_adjust import load_segments, map_offset, unadjust_price

#: 信号基于小时线 → 反解用 min60 段表（与 back_adjust.TABLE 对齐；实测 roll_segment 含 min60）
SIGNAL_FREQ = "min60"
#: 默认价空间：raw=原始连续价（888 序列实测如此）；adj=已后复权价
DEFAULT_PRICE_SPACE = __import__("os").getenv("EXECUTION_PRICE_SPACE", "raw")


def offset_at(session, symbol: str, when: datetime, freq: str = SIGNAL_FREQ) -> float:
    """返回 symbol 在 when 时刻的 cum_offset（as-of join 末段视为无穷大）。

    取不到段表（如 symbol 无 roll_segment）→ 返回 0.0。
    """
    segs = load_segments(session, symbol, freq)
    if segs is None or segs.empty:
        return 0.0
    # 时区对齐：DB 的 seg_start 多为 timestamptz（带 tz），而 when 可能 naive，
    # 必须先统一 tz 再交给 map_offset，否则 numpy 比较会抛 tz-naive/aware 错误。
    ts = pd.Series([pd.Timestamp(when)])
    seg_tz = segs["seg_start"].dt.tz
    if seg_tz is not None and ts.dt.tz is None:
        ts = ts.dt.tz_localize(seg_tz)
    elif seg_tz is None and ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    off = map_offset(ts, segs)
    return float(off.iloc[0])


def reverse_price(session, symbol: str, adj_px: float, when: datetime,
                  price_space: str | None = None) -> tuple[float, float]:
    """反解单个价：返回 (real_px, cum_offset)。

    price_space:
      - "adj"：real = adj_px - cum_offset(t)（已后复权价空间）
      - "raw" 或 None（默认）：real = adj_px（原始连续价空间，888 序列实测如此）
    """
    space = (price_space or DEFAULT_PRICE_SPACE).lower()
    if space == "adj":
        cum = offset_at(session, symbol, when)
        return unadjust_price(adj_px, cum), cum
    # raw：entry_px 已在真实合约原始价空间，偏移置 0
    return adj_px, 0.0
