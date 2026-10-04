# -*- coding: utf-8 -*-
"""P0-1 组件①（参考实现）：价格反解 —— 连续价 → 真实合约原始价。

这是执行反解层的**数学核心**，也是"反解错则实盘错"的最高风险点，故作为
CB 落地的参考范本：接口稳定、边界显式、可独立运行 selfcheck 验证。

------------------------------------------------------------------
口径决策（2026-10-02 探针结论，务必先读）
------------------------------------------------------------------
hourly_bar.<SYM888>.close 是**原始连续价**（未叠加 cum_offset）。
例如 RB888 收盘 3112、cum_offset=2400；若 3112 已后复权则 real=712（螺纹不可能），
故 888 序列为原始连续价 → 默认 price_space="raw"，偏移置 0，real = adj_px。

对应两种价空间：
  - "raw"（默认）：entry_px 已在真实合约原始价空间，real = adj_px，offset 置 0。
  - "adj"        ：real = adj_px - cum_offset(t)，需 as-of 取偏移。

adjoint 偏移取数复用 app.data.back_adjust：
  - unadjust_price(adj_px, cum_offset)        # 纯数学 real = adj - offset
  - load_segments(session, sym, freq)         # 取分段表
  - map_offset(ts, segs)                       # as-of join 末段视为无穷大

------------------------------------------------------------------
as-of 偏移语义（与 back_adjust.map_offset 一致）
------------------------------------------------------------------
段定义 [seg_start, seg_end)；最后一段 seg_end 为空 → 视为无穷大。
落在所有段之前（早于首段）→ 偏移 0。
时点恰好 = seg_start → 取该段（searchsorted side='right' - 1）。

本模块为「参考实现」，可独立运行验证：
    PYTHONPATH=. python -m app.execution.reverse_price
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from app.data.back_adjust import load_segments, map_offset, unadjust_price

#: 信号基于小时线 → 反解用 min60 段表（与 back_adjust.TABLE / roll_segment 对齐）
SIGNAL_FREQ = "min60"
#: 默认价空间：raw=原始连续价（888 序列实测如此）；adj=已后复权价
DEFAULT_PRICE_SPACE = __import__("os").getenv("EXECUTION_PRICE_SPACE", "raw")


class ReverseResult:
    """单点反解结果（轻量、无外部依赖，便于编排层与测试消费）。"""

    __slots__ = ("real_px", "offset", "space", "warning")

    def __init__(self, real_px: float, offset: float, space: str,
                 warning: Optional[str] = None):
        self.real_px = real_px
        self.offset = offset
        self.space = space
        self.warning = warning

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"ReverseResult(real_px={self.real_px}, offset={self.offset}, "
                f"space={self.space!r}, warning={self.warning!r})")


def offset_at(session, symbol: str, when: datetime, freq: str = SIGNAL_FREQ) -> float:
    """返回 symbol 在 when 时刻的 cum_offset（as-of join 末段视为无穷大）。

    取不到段表（如 symbol 无 roll_segment 或 DB 异常）→ 返回 0.0 并附带告警由调用方收集。
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


def reverse_bar(session, symbol: str, bar: dict, when: datetime,
                price_space: str | None = None,
                cols: tuple[str, ...] = ("open", "high", "low", "close")) -> dict:
    """批量反解一根 K 线的多个价位（OHLC）。

    bar: 含 o/h/l/c 价格的 dict（键名由 cols 指定）；返回同结构 dict，
    并附 "_offset" 与 "_space" 字段。single-point 接口的批量形态，反解整根 K 必用，
    否则高/低/收各自独立调 reverse_price 会重复查库且可能取到不同段边界。
    """
    space = (price_space or DEFAULT_PRICE_SPACE).lower()
    out: dict = {}
    if space == "adj":
        cum = offset_at(session, symbol, when)
        for c in cols:
            if c in bar and bar[c] is not None:
                out[c] = unadjust_price(float(bar[c]), cum)
        out["_offset"] = cum
    else:
        for c in cols:
            if c in bar and bar[c] is not None:
                out[c] = float(bar[c])
        out["_offset"] = 0.0
    out["_space"] = space
    return out


# ----------------------------------------------------------------------------
# 独立自测（DB-free）：用 monkeypatch 注入假分段表，验证 as-of 数学与两价空间分支。
# 运行：PYTHONPATH=. python -m app.execution.reverse_price
# ----------------------------------------------------------------------------
def _fake_segments_rb888() -> pd.DataFrame:
    """确定性假分段：RB888 min60，三段，cum_offset 阶梯递增。"""
    rows = [
        # seg_no, seg_start,                seg_end,                  roll_ts, roll_delta, cum_offset, price_shift, n_bars
        (0, pd.Timestamp("2020-01-01"), pd.Timestamp("2023-01-01"), None,    None,      0.0,        0.0,         None),
        (1, pd.Timestamp("2023-01-01"), pd.Timestamp("2025-01-01"), None,    None,      2400.0,     0.0,         None),
        (2, pd.Timestamp("2025-01-01"), None,                       None,    None,      5000.0,     0.0,         None),
    ]
    return pd.DataFrame(rows, columns=["seg_no", "seg_start", "seg_end", "roll_ts",
                                        "roll_delta", "cum_offset", "price_shift", "n_bars"])


def selfcheck() -> list[str]:
    """返回断言通过/失败信息列表；空列表表示全部通过。

    DB-free：直接 patch 当前模块全局 ``load_segments``（用 ``globals()`` 而非
    按名 import，规避 ``python -m`` 双模块对象导致 patch 错模块的坑）。
    """
    log: list[str] = []
    g = globals()
    orig = g["load_segments"]

    def fake_load(*_a, **_k):
        return _fake_segments_rb888()

    def empty_load(*_a, **_k):
        return pd.DataFrame(columns=["seg_no", "seg_start", "seg_end", "roll_ts",
                                     "roll_delta", "cum_offset", "price_shift", "n_bars"])

    try:
        g["load_segments"] = fake_load

        # 1) raw 空间：偏移恒 0，real = adj_px
        r, c = reverse_price(None, "RB888", 3112.0, pd.Timestamp("2024-06-01"), "raw")
        assert (r, c) == (3112.0, 0.0), f"raw 分支失败: {(r, c)}"
        log.append("✓ raw 空间：real=adj_px, offset=0")

        # 2) adj 空间 as-of：2024-06-01 落在 seg1(cum_offset=2400)
        r, c = reverse_price(None, "RB888", 5500.0, pd.Timestamp("2024-06-01"), "adj")
        assert (r, c) == (3100.0, 2400.0), f"adj seg1 失败: {(r, c)}"
        log.append("✓ adj 空间 as-of：2024-06-01 → offset=2400, real=3100")

        # 3) adj 空间 tie-break：时点恰好 = seg_start(2023-01-01) 取该段(2400)
        r, c = reverse_price(None, "RB888", 5500.0, pd.Timestamp("2023-01-01"), "adj")
        assert (r, c) == (3100.0, 2400.0), f"tie-break 失败: {(r, c)}"
        log.append("✓ adj 空间 tie-break：when==seg_start 取该段")

        # 4) adj 空间早于首段：偏移 0
        r, c = reverse_price(None, "RB888", 5500.0, pd.Timestamp("2019-01-01"), "adj")
        assert (r, c) == (5500.0, 0.0), f"早于首段失败: {(r, c)}"
        log.append("✓ adj 空间早于首段：offset=0")

        # 5) 缺段表（load_segments 返回空）→ 偏移 0，adj 安全降级
        g["load_segments"] = empty_load
        r, c = reverse_price(None, "RB888", 3112.0, pd.Timestamp("2024-06-01"), "adj")
        assert (r, c) == (3112.0, 0.0), f"空段表降级失败: {(r, c)}"
        log.append("✓ adj 空间缺段表：安全降级 offset=0（不抛异常）")

        # 6) reverse_bar 批量：整根 K 共享同一 offset
        g["load_segments"] = fake_load
        bar = reverse_bar(None, "RB888",
                          {"open": 5400.0, "high": 5600.0, "low": 5300.0, "close": 5500.0},
                          pd.Timestamp("2024-06-01"), "adj")
        assert bar["close"] == 3100.0 and bar["open"] == 3000.0 and bar["_offset"] == 2400.0, f"reverse_bar 失败: {bar}"
        log.append("✓ reverse_bar 批量：OHLC 共享 offset=2400")
    except AssertionError as e:
        log.append(f"❌ 断言失败: {e}")
    finally:
        g["load_segments"] = orig
    return log


if __name__ == "__main__":  # pragma: no cover
    results = selfcheck()
    if results:
        print("[selfcheck] 通过项：")
        for line in results:
            print("  " + line)
        print(f"[selfcheck] 全部 {len(results)} 项通过 ✅")
    else:
        print("[selfcheck] 无任何检查运行（异常）❌")
