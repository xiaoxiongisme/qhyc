# -*- coding: utf-8 -*-
"""CL-20261110-15 回归：roll_yield 零写入不再假绿 + 因子逐交易日覆盖判据。

事故复现（2026-11-10 生产实证）
--------------------------------
* 11-09 ``collect_roll_yield`` 记 success，``roll_yield`` 当日 0 行；
  次日靠 ``max(report_date)+1`` 续跑才补回 47 行。
* ``factor_freshness_check.py --report-only`` 只报 3 个断供，漏掉
  ``f_atr_pctile``：11-07 残留 11 行把 ``max(trade_date)`` 顶住，``lag=3``
  不满足 ``lag > 3``；而 11-09/11-10 逐日 0 行整表 max 根本看不见。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    # Python 3.13 的 dataclasses 解析 string annotation 时会查 sys.modules；
    # 动态加载的模块若不注册，@dataclass class Ctx 会抛 AttributeError。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def roll():
    return _load("_t_collect_roll_yield", "scripts/collect_roll_yield.py")


@pytest.fixture(scope="module")
def fresh():
    return _load("_t_factor_freshness", "scripts/factor_freshness_check.py")


@pytest.fixture(scope="module")
def guard():
    return _load("_t_guard_invariants", "scripts/ops/guard_invariants.py")


class _Cur:
    """按 SQL 分派返回值的假游标，覆盖 fetch_freshness / fetch_daily_coverage。"""

    def __init__(self, cov_rows, fresh_rows):
        self.cov_rows, self.fresh_rows = cov_rows, fresh_rows
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.cov_rows if "CROSS JOIN td" in self.sql else self.fresh_rows


class _R:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _S:
    def __init__(self, rows=None):
        self.rows = rows or []

    def execute(self, q, p=None):
        return _R(self.rows)


def _days(n: int = 5) -> list[date]:
    return [date(2026, 11, 3) + timedelta(days=i) for i in range(n)]


def _cov(vals, fid: str = "f_atr_pctile"):
    return [(fid, d, n) for d, n in zip(_days(len(vals)), vals)]


# ------------------------------------------------------------------ E1
@pytest.mark.parametrize("res,want", [
    ({"RB": 0, "CU": 0}, 0),
    ({"RB": 47, "CU": 0}, 47),
    ({"RB": "47(db_fallback)"}, 47),
    ({"RB": "失败: Timeout"}, 0),
    ({}, 0),
    ({"RB": 1.0, "CU": 2}, 3),
])
def test_day_written_rows(roll, res, want):
    assert roll.day_written_rows(res) == want


def test_require_rows_wired_in_scheduler(roll):
    """调度必须真的传 --require-rows，否则脚本改了没人用。"""
    import inspect

    from app import scheduler

    assert "--require-rows" in inspect.getsource(scheduler._collect_roll_yield_job)
    src = inspect.getsource(roll.main)
    assert "--require-rows" in src and "written == 0" in src and "fails += 1" in src


# ------------------------------------------------------------------ E2/E3
@pytest.mark.parametrize("vals,mn,want", [
    ([80, 81, 80, 80, 79], 1, 0),
    ([80, 81, 80, 80, 0], 1, 1),
    ([80, 0, 80, 0, 0], 1, 1),
    ([0, 0, 0, 0, 0], 1, 0),
    ([80, 11, 80, 80, 80], 20, 1),
    ([80, 11, 80, 80, 80], 1, 0),
])
def test_coverage_gaps(fresh, vals, mn, want):
    assert len(fresh.coverage_gaps(_cov(vals), mn)) == want


def test_coverage_gap_reports_missing_day(fresh):
    gaps = fresh.coverage_gaps(_cov([80, 81, 80, 80, 0]), 1)
    assert len(gaps) == 1
    assert gaps[0]["factor_id"] == "f_atr_pctile"
    assert gaps[0]["reason"] == "coverage_gap"
    assert "2026-11-07" in gaps[0]["missing_days"]


def test_lag_boundary_blindspot_is_covered(fresh):
    """lag=3 恰好不触发 lag>3，逐日 0 行仍必须把它送进补算列表。"""
    cur = _Cur(_cov([80, 81, 80, 80, 0]),
               [("f_atr_pctile", date(2026, 11, 7), 137647)])
    need = fresh.needs_attention(cur, 3, date(2026, 11, 10))
    assert "f_atr_pctile" in [x["factor_id"] for x in need]
    item = next(x for x in need if x["factor_id"] == "f_atr_pctile")
    assert "coverage_gap" in item["reason"]


def test_long_window_group_for_atr_pctile(fresh):
    assert "f_atr_pctile" in fresh.LONG_HISTORY_FACTORS
    assert fresh._group_of("f_atr_pctile") == "v1v6"


# ------------------------------------------------------------------ 守卫
def test_guard_assertions_pass(guard):
    ctx = guard.Ctx(session=_S())
    for fn, key in [
        (guard._c_roll_yield_zero_loud, "OPS-ROLL-YIELD-ZERO-LOUD"),
        (guard._c_factor_freshness_coverage, "OPS-FACTOR-FRESHNESS-COVERAGE"),
    ]:
        ok, expected, observed, detail = fn(ctx)
        assert ok, f"{key} FAIL: {observed} | {detail}"
    keys = [x[0] for x in guard.INVARIANTS]
    assert "OPS-ROLL-YIELD-ZERO-LOUD" in keys
    assert "OPS-FACTOR-FRESHNESS-COVERAGE" in keys
