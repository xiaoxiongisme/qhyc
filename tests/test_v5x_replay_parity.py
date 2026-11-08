# -*- coding: utf-8 -*-
"""V5X 回放补录与实时扫描的一致性（CL-20261108-28）。

回放补录的价值全在「它复现的就是当时会推的东西」。一旦回放和实时走了两套逻辑，
就会出现最难查的一类问题：对账报告很漂亮、实盘完全对不上，而两边都不报错。

这组测试钉四件事：
  1. **每根 bar 用自己那根的复权偏移**（BA 是加性偏移，随时间漂移；
     拿最后一根的偏移去反解全部历史会把早年价格算成负几万）；
  2. ``hours_only`` 只保留 SCAN_HOURS 的 bar —— 12:00 那根实时从未被扫过，
     回放若纳入就会凭空多出实盘不存在的信号；
  3. 区间边界是闭区间，且按 bar 收盘时刻判；
  4. 预热门与实时一致：该 bar 之前不足 MIN_BARS 根就不产信号。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.strategies.v5x import live


def _bars() -> pd.DataFrame:
    """5 根 bar：09/10/12/13/14 点，偏移逐根递增（模拟 BA 偏移漂移）。"""
    buckets = pd.to_datetime([
        "2026-11-06 09:00", "2026-11-06 10:00", "2026-11-06 12:00",
        "2026-11-06 13:00", "2026-11-06 14:00",
    ])
    real = np.array([3500.0, 3510.0, 3520.0, 3530.0, 3540.0])
    off = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
    return pd.DataFrame({
        "symbol": ["RB888"] * 5,
        "bucket": buckets,
        "open": real + off,
        "high": real + 5 + off,
        "low": real - 5 + off,
        "close": real + off,
        "volume": [100.0] * 5,
        "raw_c": real,
    })


class _FakeEngine:
    """每根 bar 产一条 OPEN 单，价格 = 该根收盘（BA 空间）。"""

    def __init__(self, *a, **kw):
        pass

    def set_daily_ctx(self, ctx):        # noqa: D401
        pass

    def on_bar(self, bucket, o, h, l, c, v, raw_c):
        return [{"type": "OPEN", "dir": 1, "price": float(c), "stop": float(c) - 10.0,
                 "lots": 1.0, "etype": "pb"}]


@pytest.fixture
def patched(monkeypatch):
    """把重放的外部依赖全部替换成可控桩。"""
    monkeypatch.setattr(live, "SymbolEngine", _FakeEngine)
    monkeypatch.setattr(live, "build_ctx", lambda md, sym, td: None)
    monkeypatch.setattr(live, "build_cost", lambda sym, d: 0.0)
    monkeypatch.setattr(live, "config_from", lambda ov: None)
    monkeypatch.setattr(live, "assign_td",
                        lambda buckets, open_days: np.array(
                            [pd.Timestamp(x).date() for x in buckets], dtype=object))
    monkeypatch.setattr(live, "MIN_BARS", 1)

    df = _bars()
    md = SimpleNamespace(bars={"RB888": df},
                         open_days=np.array([np.datetime64("2026-11-06")]))
    monkeypatch.setattr(live, "_load", lambda session, symbols, end: (md, ["RB888"]))
    return df


def test_replay_orders_uses_own_offset(patched):
    """★ 每根 bar 的偏移独立，不能拿最后一根的偏移反解全部历史。"""
    df = patched
    md = SimpleNamespace(bars={"RB888": df}, open_days=np.array([]))
    seq = list(live._replay_orders(md, "RB888", df, None))

    assert [round(o[2], 2) for o in seq] == [0.0, 10.0, 20.0, 30.0, 40.0], \
        "复权偏移必须逐根取，取末根会把历史价格整体算错"


def test_scan_range_filters_non_scan_hours(patched):
    """12:00 不在 SCAN_HOURS → 回放默认必须排除（实时从未扫过那根）。"""
    assert 12 not in live.SCAN_HOURS
    start = datetime(2026, 11, 6, 9, 0)
    end = datetime(2026, 11, 6, 14, 30)

    got = live.scan_range(None, start=start, end=end)
    hours = sorted({pd.Timestamp(s.bar_ts).hour for s in got})
    assert hours == [9, 10, 13, 14], f"12 点那根应被排除，实际 {hours}"

    got_all = live.scan_range(None, start=start, end=end, hours_only=False)
    assert len(got_all) == 5, "hours_only=False 时应包含 12 点那根（仅供诊断）"


def test_scan_range_bounds_inclusive(patched):
    """区间是闭区间，按 bar 收盘时刻判。"""
    got = live.scan_range(None,
                          start=datetime(2026, 11, 6, 10, 0),
                          end=datetime(2026, 11, 6, 13, 0))
    ts = [pd.Timestamp(s.bar_ts).strftime("%H:%M") for s in got]
    assert ts == ["10:00", "13:00"], f"闭区间边界应含 10:00 与 13:00，实际 {ts}"


def test_scan_range_matches_scan_once_at_same_bar(patched):
    """同一根 bar：回放结果 == 实时 scan_once 当时会推的那条。"""
    now = datetime(2026, 11, 6, 10, 5)      # 10:00 那根刚收盘
    once = live.scan_once(None, symbols=["RB888"], now=now)
    assert len(once) == 1

    rng = live.scan_range(None, symbols=["RB888"],
                          start=datetime(2026, 11, 6, 9, 0),
                          end=now)
    last = sorted(rng, key=lambda s: s.bar_ts)[-1]

    assert pd.Timestamp(last.bar_ts) == pd.Timestamp(once[0].bar_ts)
    assert last.price_real == pytest.approx(once[0].price_real)
    assert last.adj_offset == pytest.approx(once[0].adj_offset)
    assert last.dedupe_key == once[0].dedupe_key, \
        "去重键不一致 → 回放补录与实盘会各记一条，统计翻倍"


def test_scan_range_respects_warmup(patched, monkeypatch):
    """预热不足（该 bar 之前不足 MIN_BARS 根）不产信号 —— 与实时同一条门。"""
    monkeypatch.setattr(live, "MIN_BARS", 3)
    got = live.scan_range(None,
                          start=datetime(2026, 11, 6, 9, 0),
                          end=datetime(2026, 11, 6, 14, 30))
    # 第 1、2 根 bar 之前分别只有 0/1 根，均 < 3 → 只剩 13:00 / 14:00（12 点被 hours 过滤）
    hours = sorted({pd.Timestamp(s.bar_ts).hour for s in got})
    assert hours == [13, 14], f"预热不足的 bar 应被跳过，实际 {hours}"


def test_dedupe_key_excludes_source():
    """dedupe_key 不含 source：先回放后实盘跑到同一根 bar 只能记一条。"""
    kw = dict(symbol="RB888", action="OPEN", side="LONG",
              bar_ts=datetime(2026, 11, 6, 10, 0),
              adj_offset=10.0, price_ba=3510.0, price_real=3500.0, lots=1.0)
    a = live.V5XSignal(**kw, source="replay")
    b = live.V5XSignal(**kw, source="live")
    assert a.dedupe_key == b.dedupe_key
