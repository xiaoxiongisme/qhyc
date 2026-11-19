# -*- coding: utf-8 -*-
"""日频派生链收敛护栏（CL-20261118-01）。

背景：2026-11-18 akshare 当日现货数据晚于 17:10 发布，``fetch_spot_basis``
空返回只 warning，作业按「函数没抛异常」记 success，导致 spot_basis /
main_contract_map / dim_main_contract_inferred 三表同步停在 T-1，而既有的
相对比较守卫全绿、直到 21:00 表级新鲜度巡检才发现。

这些测试不连数据库：用桩替换 session 与抓取函数，验证「当日缺行必须
fail-loud」「已齐则幂等跳过」「收敛链路真实接线」「绝对基准守卫已注册」。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "ops" / "reconcile_daily_derived.py"

# 与 test_main_contract_infer.py 相同的加载方式：模块顶层会 import app.*，
# 故需在容器内（PYTHONPATH=/app）执行；本地只做语法编译。
_spec = importlib.util.spec_from_file_location("daily_derived_under_test", _SCRIPT)
rd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rd
_spec.loader.exec_module(rd)

D = dt.date(2026, 11, 18)


class _Res:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val


class _Session:
    """按 SQL 关键字把 count(*) / EXISTS 映射到可控结果。"""

    def __init__(self, rows_by_table: dict[str, int], trade_day: bool = True):
        self.rows_by_table = rows_by_table
        self.trade_day = trade_day

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        if "futures_rule" in s and "EXISTS" in s:
            return _Res(1 if self.trade_day else 0)
        for key, n in self.rows_by_table.items():
            if key in s:
                return _Res(n)
        return _Res(0)


def test_today_rows_counts_only_target_date(monkeypatch):
    """today_rows 只统计 end_day 的行，且按 (date, symbol, version) 去重。"""
    import app.ingest.spot_basis as sb

    rows = [
        {"report_date": D, "symbol": "A", "version": "v1.0", "spot_price": 1},
        {"report_date": D, "symbol": "A", "version": "v1.0", "spot_price": 2},  # 重复键
        {"report_date": D - dt.timedelta(days=1), "symbol": "B",
         "version": "v1.0", "spot_price": 3},
    ]
    monkeypatch.setattr(sb, "fetch_spot_basis", lambda *a, **k: rows)
    monkeypatch.setattr(sb, "upsert_spot_basis", lambda s, r: len(r))

    class S:
        def commit(self):
            pass

    stats = sb.collect_spot_basis(S(), D - dt.timedelta(days=5), D)
    assert stats["rows"] == 3
    assert stats["today_rows"] == 1, "当日两行同键应去重为 1，且不含 T-1 行"


def test_require_today_raises_when_missing(monkeypatch):
    """核心回归：当日 0 行但窗口其它日有数 —— 旧实现会静默记 success。"""
    import app.ingest.spot_basis as sb

    rows = [{"report_date": D - dt.timedelta(days=1), "symbol": "A",
             "version": "v1.0", "spot_price": 3}]
    monkeypatch.setattr(sb, "fetch_spot_basis", lambda *a, **k: rows)
    monkeypatch.setattr(sb, "upsert_spot_basis", lambda s, r: len(r))

    class S:
        def commit(self):
            pass

    assert sb.collect_spot_basis(S(), D - dt.timedelta(days=5), D)["today_rows"] == 0
    with pytest.raises(RuntimeError, match="禁止记为 success"):
        sb.collect_spot_basis(S(), D - dt.timedelta(days=5), D,
                             require_today=True, min_today_rows=30)


def test_require_today_ok_when_present(monkeypatch):
    """当日够数时不抛，且默认参数保持向后兼容（手动触发入口不受影响）。"""
    import app.ingest.spot_basis as sb

    rows = [{"report_date": D, "symbol": f"V{i}", "version": "v1.0"}
            for i in range(30)]
    monkeypatch.setattr(sb, "fetch_spot_basis", lambda *a, **k: rows)
    monkeypatch.setattr(sb, "upsert_spot_basis", lambda s, r: len(r))

    class S:
        def commit(self):
            pass

    stats = sb.collect_spot_basis(S(), D - dt.timedelta(days=5), D,
                                  require_today=True, min_today_rows=30)
    assert stats["today_rows"] == 30


def test_today_rows_reads_all_three_tables():
    """收敛作业必须同时看三张表，漏一张就会重演 T-1 链条断更。"""
    s = _Session({"l0_raw.spot_basis": 50,
                  "l3_ref.main_contract_map": 50,
                  "l3_ref.dim_main_contract_inferred": 50})
    out = rd._today_rows(s, D)
    assert set(out) == {"spot_basis", "main_contract_map", "inferred"}
    assert all(v == 50 for v in out.values())


def test_is_trade_day_uses_futures_rule():
    """禁止 weekday 猜测：必须查 futures_rule 真源。"""
    import re
    assert rd._is_trade_day(_Session({}, True), D) is True
    assert rd._is_trade_day(_Session({}, False), D) is False
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "SELECT EXISTS(SELECT 1 FROM futures_rule WHERE trade_date = :d)" in src
    # 注释里出现「禁止 weekday 猜测」是合法的；只禁止真的调用 weekday()
    assert not re.search(r"\.weekday\s*\(", src), "不得用 weekday() 猜交易日"


def test_reconcile_is_idempotent_when_all_present(monkeypatch):
    """三表已齐时应直接返回 0，且不触发任何写库子进程。"""
    calls = []
    monkeypatch.setattr(rd, "session_scope",
                        lambda: _FakeCtx(_Session({"l0_raw.spot_basis": 50,
                                                   "l3_ref.main_contract_map": 50,
                                                   "l3_ref.dim_main_contract_inferred": 50})))
    monkeypatch.setattr(rd, "_run_cli", lambda *a, **k: calls.append(a))
    assert rd.main(["--date", D.isoformat()]) == 0
    assert calls == [], "已齐的交易日不应重复派生"


class _FakeCtx:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        return False


def test_writer_chain_and_fail_loud_not_removed():
    """写入方不许消失：级联三步 + check=True + 收敛后复核必须都在。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "collect_spot_basis" in src and "require_today=True" in src
    assert "scripts/refresh_main_contract_map.py" in src
    assert "scripts/ops/infer_main_contract.py" in src
    assert '"--apply"' in src or "'--apply'" in src
    assert "check=True" in src, "吞掉 returncode 会让失败静默（skill 坑 #9）"
    assert "禁止记为成功" in src, "收敛后仍缺必须硬失败"
    assert "MIN_ROWS" in src


def test_guard_invariants_registered():
    """绝对新鲜度断言必须以 block 级注册，防再次被相对比较掩盖。"""
    src = (_REPO / "scripts" / "ops" / "guard_invariants.py").read_text(
        encoding="utf-8")
    assert '("DATA-DAILY-DERIVED-FRESH"' in src
    assert '"block", _c_daily_derived_fresh)' in src
    assert "def _c_daily_derived_fresh" in src
    assert "_expected_trade_day" in src, "必须用交易日真源做绝对基准"
    assert "cfg_calendar_seed_meta" in src, "必须防真源同步落后"
    assert "DATA-MAIN-CONTRACT-INFER-FRESH" in src, "旧相对断言保留、不删证据链"


def test_scheduler_marks_zero_today_rows():
    """17:10 主作业必须把「当日 0 行」写进流水，不许静默 success。"""
    src = (_REPO / "app" / "scheduler.py").read_text(encoding="utf-8")
    assert "today_rows" in src
    assert "daily_derived_reconcile" in src
    assert "当日 0 行" in src


def test_cron_window_after_infer_before_alert():
    """收敛时刻须落在 (18:30, 21:00)，与守卫判据一致。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "20:30" in src
    assert "settle_cutoff_hour" in src
