# -*- coding: utf-8 -*-
"""CL-20261124-01：V5X 快照展示层换价（分钟真实价）单测。

核心断言：只改 last_price/unrealized_yuan，stop_real/avg/dir/lots/bar_last_price 不变。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.strategies.v5x import snapshot


def _snap(price_source="minute", last_price=148.05, bar_last=148.45):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    return {
        "asof": "2026-11-24",
        "last_bar": "2026-11-24 15:00:00+08:00",
        "stale_days": 0,
        "n_long": 0, "n_short": 1, "n_flat": 0,
        "rows": [{
            "symbol": "BR888", "dir": "SHORT", "lots": 1,
            "avg_px_real": 147.40, "entry_px_real": 147.40,
            "last_price": last_price, "bar_last_price": bar_last,
            "price_source": price_source,
            "price_ts": ts if price_source == "minute" else None,
            "stop_real": 151.888, "unrealized_yuan": -325.0,
            "be_done": False, "entry_bar": "2026-11-23 11:15:00+08:00",
            "last_bar": "2026-11-24 15:00:00+08:00", "price_ba": 146.80,
        }],
    }


def test_format_snapshot_marks_minute_source():
    text = snapshot.format_snapshot(_snap())
    assert "现价取分钟实时价" in text
    assert "止损/均价按 60m 桶" in text
    assert "现148." in text and "现148.4" not in text  # 展示价用分钟价 148.05，非 60m 桶 148.45


def test_format_snapshot_bar_source_has_no_live_note():
    text = snapshot.format_snapshot(_snap(price_source="bar_60m"))
    assert "现价取分钟实时价" not in text


def test_latest_real_prices_empty_symbols():
    class S:
        def execute(self, *a, **k):
            raise AssertionError("空 symbols 不应查库")
    assert snapshot.latest_real_prices(S(), []) == {}


def test_use_live_price_false_flag_exists():
    import inspect
    sig = inspect.signature(snapshot.theoretical_positions)
    assert "use_live_price" in sig.parameters
    assert sig.parameters["use_live_price"].default is True


def test_price_fields_are_separated_in_source():
    """守卫同源：stop_real 不得取自分钟价，bar_last_price 必须独立存在。"""
    import ast
    from pathlib import Path
    src = Path(snapshot.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "theoretical_positions")
    segment = ast.get_source_segment(src, fn) or ""
    assert "stop_real = _cur_stop(eng) - off" in segment, "止损仍须由 60m 回放引擎决定"
    assert "bar_last_price" in segment, "必须保留 60m 口径对账字段"
    assert '"last_price": round(show_price' in segment, "展示价才用 show_price"
