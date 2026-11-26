# -*- coding: utf-8 -*-
"""CL-20261126-02 · 合约清单陈旧批次拒绝 + 日内新鲜度判据回归。

生产实证：11-25 22:15 上游快照返回 558 个 2024/2025 合约，覆盖 676 个 2026 合约；
watermarked=0 → 单轮 940~1015s > 15min 调度间隔 → flock -n 失败 11 次。
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest


NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


def test_plausible_universe_rejects_stale_batch():
    from app.ingest.contract_universe import plausible_universe

    rows = [{"symbol": f"A{yy}03"} for yy in (24, 24, 25)] * 40
    ok, why = plausible_universe(rows, now=NOW)
    assert not ok
    assert "有效月份占比" in why


def test_plausible_universe_accepts_current_batch():
    from app.ingest.contract_universe import plausible_universe

    rows = ([{"symbol": "A2612"}] * 50 + [{"symbol": "A2701"}] * 50)
    ok, why = plausible_universe(rows, now=NOW)
    assert ok, why


def test_plausible_universe_rejects_too_small_batch():
    from app.ingest.contract_universe import plausible_universe

    ok, why = plausible_universe([{"symbol": "A2701"}] * 10, now=NOW)
    assert not ok
    assert "合约数" in why


def test_refresh_wires_gate_and_fail_loud(monkeypatch):
    """refresh 必须先过闸门；闸门拒绝时不得调用 sync_universe。"""
    from app.ingest import contract_universe as uni

    monkeypatch.setattr(uni, "fetch_universe", lambda **kw: [{"symbol": "A2403"}] * 120)
    monkeypatch.setattr(uni, "_delisted", lambda s: set())
    called = []
    monkeypatch.setattr(uni, "sync_universe", lambda s, rows: called.append(rows) or 1)

    with pytest.raises(RuntimeError, match="拒绝同步可疑合约清单"):
        uni.refresh(object())
    assert called == []

    n = uni.refresh(object(), allow_stale=True)
    assert n == 1 and len(called) == 1


def test_refresh_ast_real_reference():
    """AST 真实调用（不看 docstring），防闸门写了却没人调。"""
    path = pathlib.Path(__file__).resolve().parents[1] / "app/ingest/contract_universe.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "refresh")
    calls = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            calls.add(n.func.id)
    assert "plausible_universe" in calls
    assert any(isinstance(n, ast.Raise) for n in ast.walk(fn))


def test_intraday_gap_probe_separates_stale_and_fresh():
    from scripts.ops.guard_invariants import _intraday_gap_ok

    base = NOW.replace(minute=0, second=0, microsecond=0)
    stale_ok, stale_why = _intraday_gap_ok(base, base - timedelta(minutes=200), base)
    fresh_ok, fresh_why = _intraday_gap_ok(base, base - timedelta(minutes=30), base)
    assert not stale_ok, stale_why
    assert fresh_ok, fresh_why


def test_intraday_gap_skips_when_continuous_stalled():
    """continuous 自身停更时交其他守卫判，避免重复报红。"""
    from scripts.ops.guard_invariants import _intraday_gap_ok

    base = NOW.replace(minute=0, second=0, microsecond=0)
    ok, why = _intraday_gap_ok(base - timedelta(minutes=120), base - timedelta(days=1), base)
    assert ok and "非活跃窗口" in why
