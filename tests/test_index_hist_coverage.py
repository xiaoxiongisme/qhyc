# -*- coding: utf-8 -*-
"""CL-20261107-11 回归：8888 指数层「历史洞」必须被断言看见。

事故：2026-11-02 / 11-03 contract 层残缺，8888 只合成出 25 / 65 个品种；
``DATA-L0-INDEX-COVERAGE`` 只看最近一个完整交易日，基准日一过就再也看不见，
换月 ratio 门静默关闭，漏检 EB/L/MA/PF/PP/PX/TA 共 7 个真实换月。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DAYS = [date(2026, 11, 9), date(2026, 11, 6), date(2026, 11, 5),
        date(2026, 11, 4), date(2026, 11, 3), date(2026, 11, 2)]


def _load(name: str = "_t_guard_index_hist"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts/ops/guard_invariants.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _R:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def scalar(self):
        if self._scalar is not None:
            return self._scalar
        return self._rows[0][0] if self._rows else None


class _S:
    """按 SQL 文本分派的最小 DB 桩，覆盖本断言的三类查询。"""

    def __init__(self, days=None, per_rows=None, scope_n=38):
        self.days = DAYS if days is None else days
        self.per_rows = ([(d, scope_n) for d in self.days]
                         if per_rows is None else per_rows)
        self.scope_n = scope_n

    def execute(self, q, p=None):
        sql = " ".join(str(q).split())
        if "SELECT DISTINCT trade_date FROM futures_rule" in sql:
            return _R([(d,) for d in self.days])
        if "WITH sc AS" in sql and "per AS" in sql:
            return _R(self.per_rows)
        if "count(*) FROM l3_ref.v_tradable_scope" in sql:
            return _R(scalar=self.scope_n)
        return _R()


def test_clean_window_passes():
    guard = _load("_t_guard_ih_1")
    ok, expected, observed, detail = guard._c_l0_index_hist_coverage(
        guard.Ctx(session=_S()))
    assert ok, f"{observed} | {detail}"
    assert "近 6 个已收盘交易日" in observed
    assert "90%" in expected


def test_historical_hole_is_caught():
    """11-02 只补出 12/38 —— 基准日已滚到 11-09 也必须报 FAIL。"""
    guard = _load("_t_guard_ih_2")
    per = [(d, 12 if d == date(2026, 11, 2) else 38) for d in DAYS]
    ok, _, observed, detail = guard._c_l0_index_hist_coverage(
        guard.Ctx(session=_S(per_rows=per)))
    assert not ok
    assert "2026-11-02 12/38" in detail
    assert "历史指数洞" in detail


def test_missing_calendar_is_loud():
    guard = _load("_t_guard_ih_3")
    ok, _, observed, detail = guard._c_l0_index_hist_coverage(
        guard.Ctx(session=_S(days=[])))
    assert not ok
    assert "seed_futures_rule" in detail


def test_scope_empty_is_loud():
    guard = _load("_t_guard_ih_4")
    ok, _, _, _ = guard._c_l0_index_hist_coverage(
        guard.Ctx(session=_S(scope_n=0)))
    assert not ok


def test_registered_as_block():
    guard = _load("_t_guard_ih_5")
    hit = [x for x in guard.INVARIANTS
           if x[0] == "DATA-L0-INDEX-HIST-COVERAGE"]
    assert len(hit) == 1
    assert hit[0][2] == "block"
    assert callable(hit[0][3])


def test_query_is_windowed_and_uses_official_calendar():
    """禁止退回 weekday 猜测，禁止对 6000 万行基表全表 GROUP BY。"""
    guard = _load("_t_guard_ih_6")
    src = inspect.getsource(guard._c_l0_index_hist_coverage)
    assert "futures_rule" in src
    assert "now() - interval '14 days'" in src
    assert "trade_date < CURRENT_DATE" in src
    assert "LIKE '%8888'" in src
