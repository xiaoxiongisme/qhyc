# -*- coding: utf-8 -*-
"""CL-20261108-06：15m 锚点映射到**更细周期**（min5）的右标签精度。

用纯合成数据（不依赖 DB），锁死三条语义：

1. min5 —— 锚点桶是右标签，桶 a 覆盖 [a-15m, a)；换月跳空发生在桶的**开盘第一笔**，
   所以边界必须落在桶内**第一根** 5m bar，而不是桶末根（旧行为）。
   PF888 2026-11-04 现场：21:05/21:10 应拿到新 offset，旧代码把它们留在旧段。
2. min5 —— 桶内一根 bar 都没有（早年 5m 数据稀疏）时，回退旧规则，事件不能丢。
3. min15/30/60 —— 不比锚点更细，行为必须逐位不变（回归保护）。
"""
import importlib.util
import pathlib

import numpy as np
import pandas as pd
import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "build_roll_segments.py"
_spec = importlib.util.spec_from_file_location("build_roll_segments", _SRC)
brs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brs)

map_roll_to_freq = brs.map_roll_to_freq


def _frames(buckets, closes):
    ts = pd.DatetimeIndex(buckets)
    g = pd.DataFrame({"bucket": ts, "close": [float(c) for c in closes]})
    return g


def _roll_idx(buckets, roll_mask):
    return [pd.Timestamp(b) for b, m in zip(buckets, roll_mask) if m]


# --- 1) 夜盘首根换月：边界落在桶内第一根 -------------------------------------
def test_min5_night_open_roll_lands_on_first_bar():
    # 15:00 日盘收（旧合约）→ 21:05 夜盘第一根（新合约，跳空 +240）
    buckets = ["2026-11-04 15:00", "2026-11-04 21:05", "2026-11-04 21:10",
               "2026-11-04 21:15", "2026-11-04 21:20"]
    closes = [7900, 8120, 8128, 8374, 8366]
    g = _frames(buckets, closes)
    # 锚点 21:15（15m 右标签桶，覆盖 [21:00,21:15)），跳空 240
    _, roll, _ = map_roll_to_freq(g, [("2026-11-04 21:15", 240.0)], 5)

    got = _roll_idx(buckets, roll)
    assert got == [pd.Timestamp("2026-11-04 21:05")], f"边界应落在桶内第一根，实际 {got}"


def test_min5_mid_session_roll_lands_on_first_bar_of_bucket():
    # 日盘 10:00 换月（15m 桶 10:15 覆盖 [10:00,10:15)）
    buckets = ["2026-11-04 09:55", "2026-11-04 10:05", "2026-11-04 10:10",
               "2026-11-04 10:15", "2026-11-04 10:20"]
    closes = [3000, 3020, 3025, 3096, 3090]
    g = _frames(buckets, closes)
    _, roll, _ = map_roll_to_freq(g, [("2026-11-04 10:15", 80.0)], 5)
    assert _roll_idx(buckets, roll) == [pd.Timestamp("2026-11-04 10:05")]


# --- 2) 桶内无 bar：回退旧规则，事件不丢 -------------------------------------
def test_min5_fallback_when_bucket_has_no_bar():
    # 早年 5m 稀疏：桶 [21:00,21:15) 内一根都没有。
    # 回退到旧规则后走 CL-15 的跨休市分支：锚点 21:15 落在 [15:00+5m, 次日 09:15)
    # 的空隙里，归「第一个 >= 锚点时刻」的桶 = 次日 09:15（不是丢掉、也不是塞进 15:00）。
    buckets = ["2026-11-04 15:00", "2026-11-05 09:15"]
    closes = [7900, 8000]
    g = _frames(buckets, closes)
    _, roll, _ = map_roll_to_freq(g, [("2026-11-04 21:15", 240.0)], 5)
    got = _roll_idx(buckets, roll)
    assert got == [pd.Timestamp("2026-11-05 09:15")], f"回退+跨休市应归次日首根，实际 {got}"


def test_min5_fallback_never_drops_event():
    # 桶内无 bar 且该周期只有一根 bar（锚点早于全部数据）→ 旧规则 drop，
    # 这是历史行为，必须保持「宁可丢也不误归」的语义，不能被 finer 分支改成乱归。
    buckets = ["2026-11-05 09:15"]
    closes = [8000]
    g = _frames(buckets, closes)
    _, roll, _ = map_roll_to_freq(g, [("2020-01-02 10:15", 50.0)], 5)
    assert not roll.any(), "锚点早于全部 5m 数据时应丢弃，不应误归到 09:15"


# --- 3) 不更细的周期：行为不变 -----------------------------------------------
def test_min15_roll_lands_on_anchor_itself():
    buckets = ["2026-11-04 15:00", "2026-11-04 21:15", "2026-11-04 21:30"]
    closes = [7900, 8374, 8354]
    g = _frames(buckets, closes)
    _, roll, _ = map_roll_to_freq(g, [("2026-11-04 21:15", 240.0)], 15)
    assert _roll_idx(buckets, roll) == [pd.Timestamp("2026-11-04 21:15")]


def test_min60_cross_break_maps_to_next_bar_not_close_bucket():
    # CL-15 回归：60m 的 15:00 → 22:00 跨休市，锚点 21:15 应归 22:00，不能吞进 15:00
    buckets = ["2026-11-04 15:00", "2026-11-04 22:00", "2026-11-04 23:00"]
    closes = [7900, 8374, 8354]
    g = _frames(buckets, closes)
    _, roll, _ = map_roll_to_freq(g, [("2026-11-04 21:15", 240.0)], 60)
    assert _roll_idx(buckets, roll) == [pd.Timestamp("2026-11-04 22:00")]


# --- 4) delta 仍用锚点 gap（各周期同一个常量）--------------------------------
def test_delta_is_anchor_gap_regardless_of_freq():
    buckets = ["2026-11-04 15:00", "2026-11-04 21:05", "2026-11-04 21:15"]
    closes = [7900, 8120, 8374]
    g = _frames(buckets, closes)
    _, _, d5 = map_roll_to_freq(g, [("2026-11-04 21:15", 240.0)], 5)
    assert np.isclose(d5.sum(), 240.0), "delta 必须是锚点 gap，不能改用本周期 cc"
