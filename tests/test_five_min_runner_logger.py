# -*- coding: utf-8 -*-
"""CL-20261125-01 · runner.py no-fresh 分支的 logger 绑定回归。

生产实证：five_min_scan 近 24h failed=4，10:05/10:35 均为
NameError: name 'logger' is not defined，源于 CL-20261124-02 在 103 行
引入 logger.warning 却未导入 logger。本测试锁死该分支可执行 + 导入常在。
"""
from __future__ import annotations

import ast
import pathlib

import pytest


def test_scan_once_no_fresh_branch_no_nameerror(monkeypatch):
    """全部品种无新鲜桶时走 no-fresh 分支：不抛 NameError，返回空列表。"""
    from app.strategies.five_min import runner

    class _Row:
        def __init__(self, status):
            self.status = status

    # 这些函数是在 scan_once 函数体内 import 的，需 patch 源模块。
    from app.data import bar5m_gate

    monkeypatch.setattr(
        bar5m_gate, "require_fresh_safe",
        lambda syms, mm, bm, **kw: ([], [_Row("IN_PROGRESS")]),
    )
    monkeypatch.setattr(bar5m_gate, "load_minute_max", lambda s, syms: {})
    monkeypatch.setattr(bar5m_gate, "load_bar5m_max", lambda s, syms: {})

    out = runner.scan_once(object(), ["EB888"], pool_id="5m-sim")
    assert out == []


def test_runner_module_has_logger_bound():
    """runner.py 顶层必须真实绑定 logger（import 或赋值），防第三处漏导。"""
    path = pathlib.Path(__file__).resolve().parents[1] / "app/strategies/five_min/runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    has_call = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"
        for n in ast.walk(tree)
    )
    assert has_call, "预期 runner.py 有 logger.xxx 调用（否则本测试失去意义）"
    bound = False
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any(a.name == "logger" for a in n.names):
            bound = True
        if isinstance(n, ast.Import) and any(a.asname == "logger" for a in n.names):
            bound = True
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "logger" for t in n.targets
        ):
            bound = True
    assert bound, "runner.py 调用了 logger.xxx 但从未绑定 logger —— 必然 NameError"
