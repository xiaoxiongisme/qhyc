# -*- coding: utf-8 -*-
"""P2 · 5M-* 九条守卫：注册与默认兼容行为单测（离线）。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "ops"))
import guard_invariants as gi  # noqa: E402


class FakeCtx:
    def __init__(self, rows=None, scalar=0):
        self._rows = rows or []
        self._scalar = scalar

    def rows(self, sql, **params):
        return self._rows

    def scalar(self, sql, **params):
        return self._scalar


EXPECTED_KEYS = {
    "5M-BAR-FRESH", "5M-BAR-GRID", "5M-COMPLETE-GATE", "5M-EXIT-CALIBER",
    "5M-LIMIT-ORDER", "5M-EXEC-GATE", "5M-SCHEDULE-PARITY",
    "5M-SINGLE-POS", "5M-RISK-R",
}


def test_nine_guards_registered_as_block():
    src = (_REPO / "scripts" / "ops" / "guard_invariants.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    registered: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "INVARIANTS":
            for elt in node.value.elts:
                k, _title, sev, fn = elt.elts
                if str(k.value).startswith("5M-"):
                    registered[k.value] = (sev.value, getattr(fn, "id", ""))
    assert set(registered) == EXPECTED_KEYS
    assert all(v[0] == "block" for v in registered.values())


def test_source_wiring_guards_pass():
    c = FakeCtx()
    for fn in (gi._c_5m_complete_gate, gi._c_5m_exit_caliber,
               gi._c_5m_risk_r, gi._c_5m_limit_order):
        ok, label, obs, detail = fn(c)
        assert ok, f"{label}: {obs} {detail}"


def test_bar_fresh_guard_catches_one_stale_symbol():
    ok, _label, _obs, detail = gi._c_5m_bar_fresh(FakeCtx(rows=[]))
    assert ok and "追平" in _obs
    stale = [("CU888", None, "2026-11-07 01:00+08", "2026-11-21 14:35+08")]
    ok, _label, obs, detail = gi._c_5m_bar_fresh(FakeCtx(rows=stale))
    assert not ok and "CU888" in detail and "异常 1 个" in obs


def test_bar_grid_guard_counts_illegal_labels():
    assert gi._c_5m_bar_grid(FakeCtx(scalar=0))[0] is True
    ok, _label, _obs, detail = gi._c_5m_bar_grid(FakeCtx(scalar=7))
    assert not ok and "7 根" in detail


def test_switched_off_guards_are_default_compatible():
    c = FakeCtx()
    for fn in (gi._c_5m_exec_gate, gi._c_5m_schedule_parity, gi._c_5m_single_pos):
        assert fn(c)[0] is True


def test_switch_on_requires_live_broker_and_single_position(monkeypatch):
    on = FakeCtx(rows=[("5m-core", 2)])
    assert gi._c_5m_single_pos(on)[0] is False
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_BROKER", "sim")
    ok, _label, _obs, detail = gi._c_5m_exec_gate(on)
    assert not ok and "broker=sim" in detail
    monkeypatch.setenv("EXECUTION_BROKER", "ctp")
    assert gi._c_5m_exec_gate(on)[0] is True
