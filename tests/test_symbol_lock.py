# -*- coding: utf-8 -*-
"""品种级 ingest 锁（symbol_ingest_lock）行为契约测试。

★ 回归锁定 2026-11-06 修复的静默吞异常缺陷：
   原实现 `finally: if not got: return` —— finally 块内的 return 对
   @contextmanager 是致命的：gen.throw() 会抛 StopIteration →
   __exit__ 返回 True → with 体内的异常被静默吞掉。

   影响链（app/ingest/orchestrator.py::ingest_symbol）：
     got=False（锁被他人持有）→ ingest_lock_holder() 抛 DB 错误 →
     异常被吞 → 函数隐式返回 None（而非 IngestReport）→
     ingest_all 结果混入 None → 下游访问 r.error 变成
     AttributeError: 'NoneType'，真正的 DB 根因完全消失。

契约：
   1) 未持锁（got=False）时，with 体内抛出的异常必须向上传播；
   2) 未持锁时不得调用 pg_advisory_unlock（否则会误解他人持有的锁）；
   3) 持锁成功时，退出上下文必须执行 pg_advisory_unlock。
"""
from __future__ import annotations

import pytest

from app.core import bootstrap


class _FakeResult:
    def __init__(self, val):
        self._v = val

    def scalar(self):
        return self._v


class _FakeConn:
    """AUTOCOMMIT 专用锁连接替身。"""

    def __init__(self, lock_granted: bool):
        self._lock_granted = lock_granted
        self.executed = []

    def execute(self, stmt, params=None):
        s = str(stmt)
        self.executed.append(s)
        if "pg_try_advisory_lock" in s:
            return _FakeResult(self._lock_granted)
        return _FakeResult(True)

    def execution_options(self, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _patch_engine(monkeypatch, granted):
    conn = _FakeConn(granted)
    monkeypatch.setattr("app.core.db.get_engine", lambda: _FakeEngine(conn))
    return conn


def test_lock_propagates_exception_when_not_acquired(monkeypatch):
    """契约 1：未持锁时 with 体内的异常必须上抛（不得被 finally 吞掉）。"""
    _patch_engine(monkeypatch, granted=False)
    with pytest.raises(RuntimeError, match="boom"):
        with bootstrap.symbol_ingest_lock("RB888"):
            raise RuntimeError("boom")


def test_lock_no_unlock_when_not_acquired(monkeypatch):
    """契约 2：未持锁时不得调用 pg_advisory_unlock。"""
    conn = _patch_engine(monkeypatch, granted=False)
    with bootstrap.symbol_ingest_lock("RB888") as got:
        assert got is False
    assert not any("pg_advisory_unlock" in s for s in conn.executed)


def test_lock_unlocks_when_acquired(monkeypatch):
    """契约 3：持锁成功时退出上下文必须执行 pg_advisory_unlock。"""
    conn = _patch_engine(monkeypatch, granted=True)
    with bootstrap.symbol_ingest_lock("RB888") as got:
        assert got is True
    assert any("pg_advisory_unlock" in s and "all" not in s for s in conn.executed)


def test_lock_propagates_exception_when_acquired(monkeypatch):
    """持锁成功时，with 体内的异常同样必须上抛（且仍要完成解锁）。"""
    conn = _patch_engine(monkeypatch, granted=True)
    with pytest.raises(RuntimeError, match="boom"):
        with bootstrap.symbol_ingest_lock("RB888"):
            raise RuntimeError("boom")
    assert any("pg_advisory_unlock" in s and "all" not in s for s in conn.executed)
