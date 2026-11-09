# -*- coding: utf-8 -*-
"""V5X 收盘即时扫描链：标签同源 / 触发窗口 / 半截桶门（CL-20261109-17）。

背景（实测，2026-11-09）：
  · `v5x_scan` 固定 :05/:35，与 60m 真实收盘标签无对应关系；
  · BA(`l2_adj.bar_*_ba`) 一天只重建 3 次（02:55/11:55/15:55），
    15:00 那根要到 15:55 才进表；
  · `synthesize_bars_incremental` 会写「在途半截桶」（14:10 已写出标签 14:15
    的半截 bar），而 `scan_once` 只按 `bucket <= now` 过滤 ⇒ 拿半根 K 线出信号，
    且 `dedupe_key` 会把错误结果永久固化。

本文件只测**纯逻辑**（无 DB）：标签集合、窗口语义、`_next_scan_time`、
半截桶门的过滤行为。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.ingest.close_pipeline import current_close_label
from app.strategies.v5x import live

SH = ZoneInfo("Asia/Shanghai")


def _t(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 11, 9, hh, mm, ss, tzinfo=SH)


# --------------------------------------------------------------- 标签集合
def test_day_close_labels_follow_synthesizer_grid():
    """日盘 60m 收盘标签必须等于合成器 `_SESSION_DAY_BOUNDS[60]` 推出的结果。

    两处各写一份的代价已经付过：30m 曾漏 14:45、5m/15m 曾整体退回自然整点桶。
    """
    from app.ingest.synthesizer import _SESSION_DAY_BOUNDS

    want = tuple(sorted((9 + b // 60, b % 60) for b in _SESSION_DAY_BOUNDS[60]))
    assert tuple(sorted(live._DAY_CLOSE_HM)) == want
    assert live._DAY_CLOSE_HM == ((10, 0), (11, 15), (14, 15), (15, 0))


def test_close_labels_cover_all_night_sessions():
    """夜盘标签是全品种并集：22:00 / 23:00 / 00:00 / 01:00 / 02:00 / 02:30。"""
    for hm in ((22, 0), (23, 0), (0, 0), (1, 0), (2, 0), (2, 30)):
        assert hm in live.CLOSE_HM, f"夜盘收盘标签缺 {hm}"
    # 并集里不应出现日盘旧网格（09:00/13:00 之类）
    assert (9, 0) not in live.CLOSE_HM and (13, 0) not in live.CLOSE_HM


# --------------------------------------------------------------- 触发窗口
@pytest.mark.parametrize("hh,mm", [(10, 0), (11, 15), (14, 15), (15, 0), (23, 0), (0, 0)])
def test_close_window_is_plus_10s(hh, mm):
    """标签 +10s 命中；+5s（太早）与 +4min（太晚，已被下一根顶掉）都不命中。"""
    label = _t(hh, mm)
    assert current_close_label(label + timedelta(seconds=live.SCAN_SECOND)) == label
    assert current_close_label(label + timedelta(seconds=5)) is None
    assert current_close_label(label + timedelta(minutes=4)) is None


def test_cross_midnight_label_belongs_to_previous_day():
    """00:00 是**当天**的标签：00:00:10 判定出的 label 不能挪到明天。"""
    now = datetime(2026, 11, 10, 0, 0, 12, tzinfo=SH)
    assert current_close_label(now) == datetime(2026, 11, 10, 0, 0, tzinfo=SH)


def test_next_scan_time_prefers_close_over_legacy_minutes():
    """14:14:30 → 14:15:10（收盘+10s），不是 14:35 的兜底位。"""
    assert live._next_scan_time(_t(14, 14, 30)) == _t(14, 15, 10)
    # 收盘后 1 秒仍在等 10 秒那次
    assert live._next_scan_time(_t(14, 15, 1)) == _t(14, 15, 10)
    # 14:20（收盘链已过）→ 兜底 :35
    assert live._next_scan_time(_t(14, 20)) == _t(14, 35)


def test_coverable_every_closed_bar_under_close_scan(monkeypatch):
    """有收盘链后，每根 bar 在自己的收盘+10s 就被评估 → `_coverable` 恒真。

    这条是「回放对账」的前提：回放若认定某根不可评估，实盘却推了，两边永远对不平。
    """
    monkeypatch.setattr(live, "is_collection_window", lambda t: True, raising=False)
    import app.ingest.trading_window as tw
    monkeypatch.setattr(tw, "is_collection_window", lambda t: True)
    for hm in ((10, 0), (11, 15), (14, 15), (15, 0)):
        bar_close = _t(*hm)
        assert live._coverable(bar_close, None) is True


# --------------------------------------------------------------- 半截桶门
class _FakeSession:
    """只实现 `_complete_max` 需要的 execute(...).all()。"""

    def __init__(self, rows):
        self._rows = rows
        self.last_sql = None

    def execute(self, sql, params=None):
        self.last_sql = str(sql)
        self.params = params

        class _R:
            def __init__(self, rows):
                self._r = rows

            def all(self):
                return self._r

        return _R(self._rows)


def test_complete_max_normalizes_tz_for_naive_now():
    """`now` 可能是 naive（`datetime.now()`），DB 回来是 aware —— 不能直接比。"""
    naive_now = datetime(2026, 11, 9, 15, 5)
    aware_ts = datetime(2026, 11, 9, 15, 0, tzinfo=SH)
    got = live._complete_max(_FakeSession([("RB888", aware_ts)]), ["RB888"], naive_now)
    assert got["RB888"].tzinfo is None
    assert got["RB888"] == datetime(2026, 11, 9, 15, 0)


def test_partial_bucket_is_dropped_by_cap():
    """分钟只到 14:07 时，标签 14:15 的半截桶必须被挡掉，最后一根退回 11:15。"""
    df = pd.DataFrame({
        "symbol": ["RB888"] * 3,
        "bucket": pd.to_datetime([
            "2026-11-09 10:00", "2026-11-09 11:15", "2026-11-09 14:15"]).tz_localize(SH),
    })
    now = _t(14, 10)
    cm = datetime(2026, 11, 9, 14, 7, tzinfo=SH)      # 分钟只覆盖到 14:07
    cap = min(now, cm)
    kept = df[df["bucket"] <= cap]
    assert len(kept) == 2
    assert kept["bucket"].iloc[-1] == pd.Timestamp("2026-11-09 11:15", tz=SH)


def test_complete_bucket_is_kept():
    """分钟已覆盖到 14:15：这根是完整 K 线，必须参与评估。"""
    df = pd.DataFrame({
        "symbol": ["RB888"],
        "bucket": pd.to_datetime(["2026-11-09 14:15"]).tz_localize(SH),
    })
    cap = min(_t(14, 16), datetime(2026, 11, 9, 14, 15, tzinfo=SH))
    assert len(df[df["bucket"] <= cap]) == 1


def test_offgrid_partial_labels_never_reach_state_machine():
    """夜盘在途半截桶的标签（21:01/21:31/22:01）必须被挡掉。

    实测 2026-11-09 夜盘：合成器夜盘分支把在途桶标签钳到 ``seg_last + 1min``，
    L1 里真的出现 21:01 / 21:30 / 21:31 / 21:38 / 22:01 这些标签。它们的 bucket
    **小于** cap（完备性门按分钟最大 ts 封顶，挡不住），若进状态机就会被当成一根
    真实的 1 分钟 K 线 —— 指标失真、信号误发，且 dedupe_key 把结果永久固化。
    """
    legal = set(live.CLOSE_HM)
    stamps = [
        "2026-11-09 21:01", "2026-11-09 21:31", "2026-11-09 22:01",  # 半截桶标签
        "2026-11-09 22:00",                                          # 合法夜盘桶
    ]
    df = pd.DataFrame({"bucket": pd.to_datetime(stamps).tz_localize(SH)})
    mask = df["bucket"].map(lambda b: (b.hour, b.minute) in legal)
    kept = df[mask]
    assert len(kept) == 1
    assert (kept["bucket"].iloc[0].hour, kept["bucket"].iloc[0].minute) == (22, 0)
