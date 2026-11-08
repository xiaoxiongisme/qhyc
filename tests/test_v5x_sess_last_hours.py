# -*- coding: utf-8 -*-
"""P6 会话末根小时集合：只能取「常态末根」，不能被残缺日污染（CL-20261109-10）。

背景：旧实现把「历史上出现过的所有当日末根小时」取并集，于是 9:00 / 13:00
这种**开盘第一根**也被当成末根 → 每天禁开仓，SF/SM 十年 0 笔成交。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.v5x.context import _sess_last_hours


def _bars(spec: dict[str, list[int]], sym="RB888") -> pd.DataFrame:
    """spec: {日期: [小时...]} → 长表（bucket 为该 bar 的**结束**时刻）。"""
    rows = [{"symbol": sym, "bucket": pd.Timestamp(f"{d} {h:02d}:00")}
            for d, hours in spec.items() for h in hours]
    return pd.DataFrame(rows)


def _open_days(days) -> np.ndarray:
    # 多给一天缓冲：夜盘 23:00 会被 assign_td 归到下一交易日
    return np.array([np.datetime64(d, "D") for d in days]
                    + [np.datetime64(pd.Timestamp(days[-1]) + pd.Timedelta(days=3), "D")])


FULL = [9, 10, 11, 13, 14, 15, 22, 23]


def test_truncated_day_does_not_pollute_session_hours():
    """个别日子缺日盘/夜盘末根，结果仍是常态的 {15, 23}。"""
    spec = {
        "2026-01-05": FULL,
        "2026-01-06": [9, 10, 11, 22, 23],      # 日盘缺 15:00 → 当日末根变 11:00
        "2026-01-07": [9, 10, 11, 13, 14, 15, 22],   # 夜盘缺 23:00 → 末根变 22:00
        "2026-01-08": FULL,
    }
    got = _sess_last_hours(_bars(spec), _open_days(list(spec)))["RB888"]
    assert got == frozenset({15, 23}), f"残缺日污染了末根集合：{sorted(got)}"


def test_session_open_hours_never_enter_the_set():
    """9:00 / 13:00 是日盘第一根，绝不能被当成末根（旧并集实现的核心缺陷）。"""
    spec = {"2026-01-05": [9, 10], "2026-01-06": FULL, "2026-01-07": FULL,
            "2026-01-08": [13, 14], "2026-01-09": FULL}
    got = _sess_last_hours(_bars(spec), _open_days(list(spec)))["RB888"]
    assert got == frozenset({15, 23}), f"开盘小时混入末根：{sorted(got)}"
    assert not got & {9, 13, 21}, "会话开始小时出现在禁开仓集合里"


def test_night_session_ending_after_midnight():
    """有色夜盘到 01:00：末根应是 {15, 1}，不是 {15, 23}。"""
    days = [f"2026-01-{x:02d}" for x in range(5, 12)]
    rows = []
    for d in days:
        for h in (10, 11, 15):                       # 日盘（末根 15）
            rows.append({"symbol": "SS888", "bucket": pd.Timestamp(f"{d} {h:02d}:00")})
        nd = pd.Timestamp(d) + pd.Timedelta(days=1)
        for h in (22, 23):                           # 夜盘前段（归属下一交易日）
            rows.append({"symbol": "SS888", "bucket": pd.Timestamp(f"{d} {h:02d}:00")})
        for h in (0, 1):                             # 跨零点后段（仍属同日）
            rows.append({"symbol": "SS888",
                         "bucket": nd.replace(hour=h, minute=0)})
    got = _sess_last_hours(pd.DataFrame(rows), _open_days(days))["SS888"]
    assert got == frozenset({15, 1}), f"跨零点夜盘末根错：{sorted(got)}"


def test_set_size_is_at_most_two():
    """一个交易日最多两个会话段 → 末根集合最多 2 个元素。"""
    spec = {f"2026-02-{d:02d}": FULL for d in range(2, 10)}
    for got in _sess_last_hours(_bars(spec), _open_days(list(spec))).values():
        assert len(got) <= 2, f"末根集合异常膨胀：{sorted(got)}"
