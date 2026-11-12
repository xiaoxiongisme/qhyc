# -*- coding: utf-8 -*-
"""CL-20261112-09：L0 实时重放护栏的契约测试。

覆盖三类回归：
1. 纯函数行为（裁切 / 交易日窗口 / 配置上限 / 异常值判据）；
2. 跨源占位过滤（含查询失败 fail-open 路径）；
3. **真实接线**（防「表建了没人读」「函数写了没人调」——本项目最贵的失效模式）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.ingest.l0_write_guard import (
    filter_cross_src_occupied,
    realtime_bars_limit,
    tail_bars,
    trading_session_bounds,
)

_SH = ZoneInfo("Asia/Shanghai")
_ROOT = Path(__file__).resolve().parents[1]


def _rows(symbol: str, hours: list[int]) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "ts": datetime(2026, 11, 12, h, 0, tzinfo=timezone.utc),
            "src": "akshare_1min",
        }
        for h in hours
    ]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """只实现 filter_cross_src_occupied 用到的 execute()。"""

    def __init__(self, occupied=(), fail=False):
        self.occupied = list(occupied)
        self.fail = fail
        self.calls: list[dict] = []

    def execute(self, stmt, params=None):
        self.calls.append(params or {})
        if self.fail:
            raise RuntimeError("db unavailable")
        wanted = {
            (s, t)
            for s, t in zip(params["syms"], params["tss"])
        }
        return _FakeResult([(s, t) for s, t in self.occupied if (s, t) in wanted])


# ---------------------------------------------------------------- 裁切
def test_tail_bars_takes_latest_after_sorting():
    rows = _rows("RB888", [11, 9, 10])  # 故意乱序
    out = tail_bars(rows, 2)
    assert [r["ts"].hour for r in out] == [10, 11]


def test_tail_bars_full_window_when_zero_or_none():
    rows = _rows("RB888", [9, 10, 11])
    assert len(tail_bars(rows, 0)) == 3
    assert len(tail_bars(rows, None)) == 3
    assert len(tail_bars(rows, 999)) == 3
    assert tail_bars([], 30) == []


def test_bars_limit_env_and_clamp(monkeypatch):
    monkeypatch.setenv("QHYC_L0_REALTIME_BARS", "30")
    assert realtime_bars_limit() == 30
    monkeypatch.setenv("QHYC_L0_REALTIME_BARS", "abc")
    assert realtime_bars_limit() == 120
    monkeypatch.setenv("QHYC_L0_REALTIME_BARS", "99999")
    assert realtime_bars_limit() == 1024


# ---------------------------------------------------------------- 跨源占位
def test_cross_src_occupied_keys_are_skipped():
    rows = _rows("RB888", [9, 10, 11])
    occupied = [("RB888", rows[0]["ts"]), ("RB888", rows[2]["ts"])]
    s = _FakeSession(occupied)
    kept, n_skipped, failed = filter_cross_src_occupied(s, rows, "akshare_1min")
    assert not failed
    assert n_skipped == 2
    assert [r["ts"].hour for r in kept] == [10]


def test_same_src_keys_are_not_reported_as_cross_src():
    """fake session 只在 occupied 列表里返回跨源行；同源行不该被跳过。"""
    rows = _rows("RB888", [9, 10])
    kept, n_skipped, failed = filter_cross_src_occupied(
        _FakeSession([]), rows, "akshare_1min"
    )
    assert (n_skipped, failed, len(kept)) == (0, False, 2)


def test_guard_query_failure_is_fail_open_and_visible():
    rows = _rows("RB888", [9, 10])
    kept, n_skipped, failed = filter_cross_src_occupied(
        _FakeSession(fail=True), rows, "akshare_1min"
    )
    assert failed is True           # 必须可见，不许静默降级
    assert n_skipped == 0
    assert len(kept) == 2          # 退回 DO NOTHING 旧行为，不阻断采集


def test_empty_rows_short_circuit():
    s = _FakeSession(fail=True)
    assert filter_cross_src_occupied(s, [], "akshare_1min") == ([], 0, False)
    assert s.calls == []


def test_chunk_pruning_predicates_are_passed():
    """hypertable 必须带 ts 上下界，否则 281 个 chunk 全扫（实测超时）。"""
    s = _FakeSession([])
    filter_cross_src_occupied(s, _rows("RB888", [9, 10]), "akshare_1min")
    assert s.calls and "lo" in s.calls[0] and "hi" in s.calls[0]
    src = (_ROOT / "app" / "ingest" / "l0_write_guard.py").read_text(
        encoding="utf-8"
    )
    assert "m.ts >= CAST(:lo AS timestamptz)" in src
    assert "m.ts <= CAST(:hi AS timestamptz)" in src


# ---------------------------------------------------------------- 交易日窗口
def test_session_bounds_regular_weekday():
    start, end, trade_date = trading_session_bounds(
        datetime(2026, 11, 12, 15, 30, tzinfo=_SH)  # 周四
    )
    assert trade_date == date(2026, 11, 12)
    assert start == datetime(2026, 11, 11, 20, 50, tzinfo=_SH)
    assert end == datetime(2026, 11, 12, 15, 20, tzinfo=_SH)


def test_session_bounds_monday_uses_friday_night():
    """周一交易日的夜盘是上周五 21:00，不能简单取「昨天 21:00」。"""
    start, _end, trade_date = trading_session_bounds(
        datetime(2026, 11, 16, 15, 30, tzinfo=_SH)  # 周一
    )
    assert trade_date == date(2026, 11, 16)
    assert start == datetime(2026, 11, 13, 20, 50, tzinfo=_SH)  # 周五


def test_session_bounds_weekend_skipped():
    assert trading_session_bounds(
        datetime(2026, 11, 14, 15, 30, tzinfo=_SH)
    ) is None
    assert trading_session_bounds(
        datetime(2026, 11, 15, 15, 30, tzinfo=_SH)
    ) is None


# ---------------------------------------------------------------- 真实接线
def test_guard_is_wired_into_both_collectors():
    mc = (_ROOT / "app" / "ingest" / "minute_collector.py").read_text(
        encoding="utf-8"
    )
    cc = (_ROOT / "app" / "ingest" / "contract_minute_collector.py").read_text(
        encoding="utf-8"
    )
    assert "tail_bars(rows, limit)" in mc
    assert "filter_cross_src_occupied(" in mc
    assert "filter_cross_src_occupied(" in cc
    assert "record_ingest_audit(" in mc


def test_eod_job_is_registered_and_never_overwrites():
    sched = (_ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
    eod = (_ROOT / "app" / "ingest" / "l0_eod_reconcile.py").read_text(
        encoding="utf-8"
    )
    assert 'id="l0_eod_reconcile"' in sched
    assert "_l0_eod_reconcile_job" in sched
    # 补缺必须 keep-first；结果表自身的 upsert 允许 DO UPDATE，
    # 故只检查行情表写入用的是 on_conflict_do_nothing。
    assert "on_conflict_do_nothing(index_elements=[\"symbol\", \"ts\"])" in eod
    assert "DO UPDATE SET src" not in eod.upper()


def test_invariants_registered():
    g = (_ROOT / "scripts" / "ops" / "guard_invariants.py").read_text(
        encoding="utf-8"
    )
    for key in (
        "OPS-L0-CROSS-SRC-QUIET",
        "OPS-L0-REPLAY-BOUNDED",
        "DATA-L0-EOD-RECONCILE",
    ):
        assert f'("{key}"' in g


@pytest.mark.parametrize(
    "row,expected",
    [
        ((None, "akshare_1min", 0, 100, 101, 99), True),      # 零成交量
        ((None, "akshare_1min", 10, 0, 101, 99), True),       # 非正收盘
        ((None, "akshare_1min", 10, 100, 98, 99), True),      # high < low
        ((None, "akshare_1min", 10, 100, 101, 99), False),    # 正常
    ],
)
def test_bad_value_detection(row, expected):
    from app.ingest.l0_eod_reconcile import _has_bad_value

    assert _has_bad_value(row) is expected
