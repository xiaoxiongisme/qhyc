# -*- coding: utf-8 -*-
"""主力合约反推口径回归护栏（CL-20261112-11）。

背景：``dim_main_contract_inferred`` 是执行层反解真实合约的首选真源，但写入方
从未进仓库/未挂调度，停在 2026-09-30；``roll_policy._resolve_inferred`` 静默
回退到陈旧记录，26/50 品种反解到与当前主力不符的合约。

这些测试不连数据库：用构造的 SQL 结果行验证「取 MAD 最小者」的组装口径，
并锁住写入链路 / 守卫断言不许再次消失（本次事故的根因就是「写入方不见了」）。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "ops", "infer_main_contract.py")
_spec = importlib.util.spec_from_file_location("main_contract_infer_under_test", _SCRIPT)
mci = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mci
_spec.loader.exec_module(mci)

D = dt.date(2026, 11, 12)


def _row(v, symbol, n, mad, ref_close, n_cand, rn):
    return (v, symbol, n, mad, ref_close, n_cand, rn)


def test_pick_min_mad_not_single_snapshot():
    """单点收盘价相等不可用，必须按整日序列 MAD 取最小者。"""
    rows = [
        _row("I", "I2701", 225, 0.0333, 671.0, 10, 1),
        _row("I", "I2702", 214, 2.2103, 671.0, 10, 2),
        _row("I", "I2612", 212, 2.2410, 671.0, 10, 3),
    ]
    out = mci._assemble(rows, {"I": "I2701"}, D)
    assert len(out) == 1
    it = out[0]
    assert it["inferred_symbol"] == "I2701"
    assert it["runner_up"] == "I2702"
    assert it["runner_up_mad"] > it["mad"]
    assert it["agrees_with_map"] is True
    assert it["n_candidates"] == 10
    assert it["method"] == "minute_bar_mad"
    assert abs(it["match_diff"] - 0.0333 / 671.0) < 1e-8


def test_rn_order_independent_and_multi_variety():
    """不依赖 SQL 的 ORDER BY；多品种各自取自己的最小 MAD。"""
    rows = [
        _row("AG", "AG2703", 225, 13.4933, 12382.0, 11, 2),
        _row("I", "I2702", 214, 2.2103, 671.0, 10, 2),
        _row("AG", "AG2702", 225, 0.4711, 12382.0, 11, 1),
        _row("I", "I2701", 225, 0.0333, 671.0, 10, 1),
    ]
    out = mci._assemble(rows, {"AG": "AG2702", "I": "I2701"}, D)
    assert [it["variety_code"] for it in out] == ["AG", "I"]
    assert [it["inferred_symbol"] for it in out] == ["AG2702", "I2701"]
    assert all(it["agrees_with_map"] for it in out)


def test_inferred_may_legally_differ_from_underlying():
    """丙/丁口径允许 888 跟随非最大持仓合约；DIFF 只记录，不是失败。"""
    rows = [
        _row("SI", "SI2612", 225, 0.2444, 8610.0, 10, 1),
        _row("SI", "SI2701", 225, 72.0444, 8610.0, 10, 2),
    ]
    out = mci._assemble(rows, {"SI": "SI2701"}, D)
    assert out[0]["inferred_symbol"] == "SI2612"
    assert out[0]["agrees_with_map"] is False
    assert out[0]["mapped_symbol"] == "SI2701"


def test_pattern_limits_to_products_and_contract_shapes():
    pat = mci._pattern(["A", "AO", "I"])
    import re
    rx = re.compile(pat)
    for good in ("A888", "AO888", "I888", "I2701", "A2612", "AO612"):
        assert rx.match(good), good
    for bad in ("X888", "SHFE.rb2612", "A8888X", "I", "rb2701"):
        assert not rx.match(bad), bad


def test_writer_chain_contract_not_removed():
    """写入方不许再消失：UPSERT + 幂等冲突 + --apply 开关必须在脚本里。"""
    src = open(_SCRIPT, encoding="utf-8").read()
    assert "INSERT INTO l3_ref.dim_main_contract_inferred" in src
    assert "ON CONFLICT (variety_code, trade_date) DO UPDATE" in src
    assert '"--apply"' in src or "'--apply'" in src
    assert "MIN_MINUTES" in src and "min_minutes" in src


def test_fallback_for_varieties_without_888_minute_data():
    """PG/PR/PS 无 X888 主连分钟线：必须显式兜底到当日 underlying，不许留空洞。

    留空洞会让 roll_policy._resolve_inferred 静默回退到几周前的陈旧合约
    （实测 PS 停在 11-06 的 PS2612，而 11-12 主力已是 PS2701）。
    """
    items = mci._assemble(
        [_row("SI", "SI2612", 225, 0.2444, 8610.0, 10, 1)], {"SI": "SI2612"}, D)
    mp = {"SI": "SI2612", "PG": "PG2701", "PR": "PR2701"}
    out = mci._with_fallback(items, ["SI", "PG", "PR"], mp, D)
    assert [it["variety_code"] for it in out] == ["PG", "PR", "SI"]
    fb = {it["variety_code"]: it for it in out
          if it["method"] == "fallback_underlying"}
    assert set(fb) == {"PG", "PR"}
    for v in ("PG", "PR"):
        assert fb[v]["inferred_symbol"] == mp[v]
        assert fb[v]["n_candidates"] == 0
        assert fb[v]["match_diff"] is None
        assert fb[v]["mad"] is None
        assert fb[v]["agrees_with_map"] is True


def test_no_fallback_when_all_varieties_resolved():
    items = mci._assemble(
        [_row("I", "I2701", 225, 0.0333, 671.0, 10, 1)], {"I": "I2701"}, D)
    out = mci._with_fallback(items, ["I"], {"I": "I2701"}, D)
    assert len(out) == 1 and out[0]["method"] == "minute_bar_mad"


def test_guard_invariants_registered():
    """两条 block 断言必须注册进 INVARIANTS（防止口径加完却没挂守卫）。"""
    guard = os.path.join(_REPO, "scripts", "ops", "guard_invariants.py")
    src = open(guard, encoding="utf-8").read()
    for key, fn in (("DATA-MAIN-CONTRACT-INFER-FRESH", "_c_main_contract_infer_fresh"),
                    ("DATA-MAIN-CONTRACT-FOLLOW", "_c_main_contract_follow")):
        assert f'("{key}"' in src, key
        assert f'"block", {fn})' in src, f"{key} 未以 block 级注册到 {fn}"
    assert "def _c_main_contract_infer_fresh" in src
    assert "def _c_main_contract_follow" in src
    assert "dim_main_contract_inferred" in src
