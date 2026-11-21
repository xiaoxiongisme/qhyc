# -*- coding: utf-8 -*-
"""CL-20261121-04 / 5M-P0A：bar_5m 逐品种完备性+新鲜度门单测（纯离线）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.data.bar5m_gate import (
    BarFreshnessError,
    complete_bucket,
    evaluate,
    filter_fresh,
    floor_5m,
    require_fresh,
)

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 21, 14, 35, tzinfo=TZ)


def test_floor_5m_and_complete_bucket():
    assert floor_5m(datetime(2026, 11, 21, 14, 37, 20, tzinfo=TZ)) == \
        datetime(2026, 11, 21, 14, 35, tzinfo=TZ)
    # 分钟 ts 是终点标签：max=14:37 ⇒ 最后收完的 5m 桶是 14:35
    assert complete_bucket(datetime(2026, 11, 21, 14, 37, tzinfo=TZ)) == \
        datetime(2026, 11, 21, 14, 35, tzinfo=TZ)
    assert complete_bucket(None) is None


def test_all_fresh_symbols_pass():
    mm = {s: datetime(2026, 11, 21, 14, 37, tzinfo=TZ) for s in ("EB888", "SR888")}
    bm = {s: datetime(2026, 11, 21, 14, 35, tzinfo=TZ) for s in mm}
    rows = evaluate(mm, mm, bm, now=NOW)
    assert all(r.ok for r in rows), [r.as_dict() for r in rows]
    assert rows[0].status == "OK"


def test_lagging_symbol_is_rejected_while_others_pass():
    """聚合 max 会漏掉的「一个品种停更」必须被逐品种门抓住。"""
    mm = {"EB888": datetime(2026, 11, 21, 14, 37, tzinfo=TZ),
          "CU888": datetime(2026, 11, 21, 14, 37, tzinfo=TZ)}
    bm = {"EB888": datetime(2026, 11, 21, 14, 35, tzinfo=TZ),
          "CU888": datetime(2026, 11, 7, 1, 0, tzinfo=TZ)}
    allowed, rows = filter_fresh(mm, mm, bm, now=NOW, max_age_min=None)
    assert allowed == ["EB888"]
    by_sym = {r.symbol: r for r in rows}
    assert by_sym["CU888"].status == "LAGGING"
    assert by_sym["CU888"].lag_minutes > 0
    with pytest.raises(BarFreshnessError, match="CU888"):
        require_fresh(mm, mm, bm, now=NOW, max_age_min=None)


def test_inflight_partial_bucket_is_rejected():
    """bar 标签跑到已收完桶之后 = 在途半截桶，必须拒绝（不是"更及时"）。"""
    mm = {"FG888": datetime(2026, 11, 21, 14, 32, tzinfo=TZ)}
    bm = {"FG888": datetime(2026, 11, 21, 14, 35, tzinfo=TZ)}
    row = evaluate(mm, mm, bm, now=NOW)[0]
    assert row.status == "AHEAD_UNSAFE" and not row.ok


def test_missing_bar_and_stale_minute_are_rejected():
    mm = {"SR888": datetime(2026, 11, 21, 14, 37, tzinfo=TZ),
          "SC888": datetime(2026, 11, 21, 14, 37, tzinfo=TZ)}
    bm = {"SR888": datetime(2026, 11, 21, 14, 35, tzinfo=TZ)}
    rows = {r.symbol: r for r in evaluate(mm, mm, bm, now=NOW)}
    assert rows["SC888"].status == "NO_BAR"
    old = {"FG888": datetime(2026, 11, 18, 10, 0, tzinfo=TZ)}
    assert evaluate(old, old, {"FG888": datetime(2026, 11, 18, 9, 55, tzinfo=TZ)},
                    now=NOW)[0].status == "STALE"


def test_naive_aware_mix_does_not_crash():
    """生产库取回 aware、调用方可能传 naive now（V5X 踩过 TypeError）。"""
    mm = {"A888": datetime(2026, 11, 21, 6, 37)}
    bm = {"A888": datetime(2026, 11, 21, 6, 35)}
    row = evaluate(mm, mm, bm, now=datetime(2026, 11, 21, 6, 40))[0]
    assert row.ok and row.age_minutes == 3
