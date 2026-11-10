# -*- coding: utf-8 -*-
"""CL-20261110-12 回归：DCE 会员持仓合约级连续性断言与覆盖修正脚本契约。

事故：2026-11-06 官网 87 个 DCE 合约里 11 个数值腰斩/行数不足，
但 `OPS-MPR-EXCHANGE-COVERAGE` / `OPS-MPR-DCE-COVERAGE` 只看品种数，
16/16 全 PASS，无人发现。
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


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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


def test_guard_clean_pass():
    guard = _load("_t_guard_dce", "scripts/ops/guard_invariants.py")
    ok, expected, observed, detail = guard._c_mpr_dce_contract_continuity(
        guard.Ctx(session=_S([])))
    assert ok, f"{observed} | {detail}"
    assert "0 处" in observed


def test_guard_collapse_is_caught():
    guard = _load("_t_guard_dce2", "scripts/ops/guard_invariants.py")
    rows = [
        (date(2026, 11, 6), "P2705", 11955, 220849, 0.054),
        (date(2026, 11, 9), "PP2701", 419909, 26249, 15.997),
    ]
    ok, _, observed, detail = guard._c_mpr_dce_contract_continuity(
        guard.Ctx(session=_S(rows)))
    assert not ok
    assert "2 处" in observed
    assert "P2705" in detail and "PP2701" in detail


def test_guard_is_registered_block():
    guard = _load("_t_guard_dce3", "scripts/ops/guard_invariants.py")
    hit = [x for x in guard.INVARIANTS if x[0] == "OPS-MPR-DCE-CONTRACT-CONTINUITY"]
    assert hit and hit[0][2] == "block"


def test_repair_script_coverage_semantics():
    """覆盖修正是 DELETE + UPSERT，不能退化成 DO NOTHING（那样修不动官网坏行）。"""
    m = _load("_t_repair_dce", "scripts/ops/repair_dce_rank_sina.py")
    sql = str(m._INSERT)
    assert "DELETE FROM" in inspect.getsource(m.repair)
    assert "ON CONFLICT" in sql and "DO UPDATE" in sql
    assert "DO NOTHING" not in sql
    assert m.BACKUP_TABLE == "l3_ref.bak_cl20261110_12"
    assert m.MIN_RATIO < 1 < m.MAX_RATIO
    src = inspect.getsource(m.repair)
    assert "verify_date_sensitive" in src          # 门 0
    assert "fetch_contract_rows" in src            # 门 1
    assert "MIN_RATIO <= ratio" in src             # 门 2


def test_repair_dry_run_default_and_backup_on_apply():
    m = _load("_t_repair_dce2", "scripts/ops/repair_dce_rank_sina.py")
    src = inspect.getsource(m.main)
    assert 'action="store_true"' in src and "--apply" in src
    assert "ensure_backup" in inspect.getsource(m.repair)
