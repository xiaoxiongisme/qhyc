# -*- coding: utf-8 -*-
"""akshare 未知品种兜底补丁护栏（CL-20261127-02）。

背景：akshare 1.18.94 ``chinese_list`` 缺「氢氧化锂」，
``futures_spot_price_daily`` 解析到该品种即抛 ValueError，重试 5 次返回 0 行，
整个日频派生链（spot_basis → main_contract_map → dim_main_contract_inferred）停更。
补丁只在调用期间包装映射函数、只兜底未知品种、退出后复原。

本测试用桩替换 akshare 映射函数，不连真实网络/数据库，验证：
  · 已知品种映射完全不变；
  · 未知品种（ValueError/KeyError）返回占位码而不抛错；
  · 上下文退出后原函数对象被复原（无全局副作用）；
  · 真实调用点在 tolerant 上下文里（AST 接线）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import akshare.futures.symbol_var as sv
import pytest

from app.ingest.spot_basis import (
    _AKSHARE_UNKNOWN_VARIETY_PLACEHOLDER as PH,
    _akshare_unknown_variety_tolerant,
)

_REPO = Path(__file__).resolve().parents[1]


def test_known_variety_mapping_unchanged():
    orig = sv.chinese_to_english
    before = orig("铜")
    with _akshare_unknown_variety_tolerant():
        inside = sv.chinese_to_english("铜")
    assert before == inside, "已知品种映射被兜底补丁改变"


def test_unknown_variety_returns_placeholder():
    with _akshare_unknown_variety_tolerant():
        assert sv.chinese_to_english("氢氧化锂") == PH
        assert sv.chinese_to_english("完全不存在的品种") == PH


def test_context_restores_original_function(monkeypatch):
    def _raise(name):
        raise ValueError(name)

    monkeypatch.setattr(sv, "chinese_to_english", _raise)
    with _akshare_unknown_variety_tolerant():
        assert sv.chinese_to_english("氢氧化锂") == PH
    assert sv.chinese_to_english is _raise, "上下文退出后未复原原函数"


def test_restore_even_on_exception(monkeypatch):
    def _raise(name):
        raise KeyError(name)

    monkeypatch.setattr(sv, "chinese_to_english", _raise)
    with pytest.raises(RuntimeError):
        with _akshare_unknown_variety_tolerant():
            assert sv.chinese_to_english("未知") == PH
            raise RuntimeError("boom")
    assert sv.chinese_to_english is _raise, "异常路径下未复原原函数"


def test_fetch_wired_to_tolerant_context():
    src = (_REPO / "app/ingest/spot_basis.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "fetch_spot_basis")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_akshare_unknown_variety_tolerant" in names
    assert "futures_spot_price_daily" in attrs


def test_placeholder_not_a_real_symbol():
    # 占位码不能撞上任何真实品种代码，否则会污染标准化结果。
    assert PH == "__UNMAPPED__"
    assert "__" in PH  # 双下划线前缀，绝非交易所品种代码形态
