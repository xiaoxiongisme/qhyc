# -*- coding: utf-8 -*-
"""E3 / P2-4（CL-20261108-17）：CI 同源断言 —— 同一 symbol+bucket，
回测与实盘读到的 K 线必须逐根相等，且两端只能有一条取数路径。

路线图 §3.6 E3 原文：
    「同一 symbol + bucket，回测与实盘读到的 bar 必须逐根相等」；
    CI 中 ``test_data_same_source`` 通过。

为什么用假 session 而不是真库
------------------------------
GitHub Actions ``test`` job 是 ubuntu-latest + Python 3.11 + ``pip install
-r requirements.txt``，**没有 postgres service**。真库测试会被 conftest 的
``requires_db`` 跳过，变成永远绿色的摆设。这里用固定 K 线回放：
  · 数值层面：两条链路各自调 ``read_fusion_kline``，交集桶逐根比对 OHLC；
  · 接线层面：AST 检查两条链路确实调用同一入口，且 walkforward 不再读
    ``HourlyBar``（E2 / CL-20261108-13 防回退）；
  · 漂移层面：SQL 白名单 + 反向哨兵 —— 若有人把 ``RAW_TABLE`` 改回
    ``l0_raw.hourly_bar``，哨兵必须能测出来（2026-11-07 P0-3 的教训）。

⚠ 本文件故意不 import ``scripts/ops/dump_fusion_profile.py``（其顶层 import
yaml，而 requirements.txt 未显式列 PyYAML），也不连库、不读配置。
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.data import fusion_kline as fk

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = "l1_mkt.bar_60m"
BA = "l2_adj.bar_60m_ba"
FORBIDDEN = "l0_raw.hourly_bar"
BASE_COLS = ["dt", "open", "high", "low", "close"]


def _bars(n: int = 260) -> list[tuple]:
    """确定性 K 线：形状不重要，重要的是两条链路拿到同一批桶。"""
    start = pd.Timestamp("2026-01-02 09:00:00")
    rows = []
    for i in range(n):
        dt = start + pd.Timedelta(hours=i)
        base = 3000.0 + (i % 37) * 1.5 + 8.0 * (i // 37)
        o = base
        c = base + ((i % 5) - 2) * 0.8
        h = max(o, c) + 1.25
        l = min(o, c) - 1.25
        rows.append((dt, o, h, l, c, None))  # 第 6 列 = adj_offset（raw 维度不用）
    return rows


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None


class _FakeSession:
    """只实现 fusion_kline 用到的 session 接口，并记录所有 SQL 供白名单核验。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        p = dict(params or {})
        self.calls.append((sql, p))
        if "min(l.bucket)" in sql:  # fusion_kline_range
            if not self.rows:
                return _Rows([(None, None)])
            return _Rows([(self.rows[0][0], self.rows[-1][0])])
        rows = self.rows
        if p.get("start") is not None:
            rows = [r for r in rows if r[0] >= p["start"]]
        if p.get("end") is not None:
            rows = [r for r in rows if r[0] <= p["end"]]
        if p.get("lim"):
            rows = list(reversed(rows[-int(p["lim"]):]))  # 模拟 ORDER BY DESC
        return _Rows(rows)


def _sql_single_source(sql: str) -> bool:
    """唯一真源 SQL：引用 L1 + BA 同表维度，绝不碰独立采集链。"""
    return RAW in sql and BA in sql and FORBIDDEN not in sql


def _func_node(path: str, func_name: str):
    src = (REPO_ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == func_name),
        None,
    )
    assert node is not None, f"{path} 中找不到函数 {func_name}"
    return src, node


def _call_names(node) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def _has_name(node, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


# ---------------------------------------------------------------------------
# 1) 数值同源：同一 symbol+bucket 逐根相等
# ---------------------------------------------------------------------------
def test_backtest_and_live_read_identical_bars():
    rows = _bars()
    sess = _FakeSession(rows)

    # 回测：显式时间窗；实盘：limit=800 滚动窗（真实调用形态，见 _read_src）
    bt = fk.read_fusion_kline(
        sess, "RB888", None, src="bar_60m",
        start=rows[0][0], end=rows[-1][0], context="backtest")
    live = fk.read_fusion_kline(
        sess, "RB888", 800, src="bar_60m", context="live")

    assert bt is not None and live is not None
    assert len(bt) == len(rows) and len(live) == len(rows)

    # context 只影响日志/元数据，不得影响数值
    assert bt.attrs["kline_context"] == "backtest"
    assert live.attrs["kline_context"] == "live"
    assert bt.attrs["kline_src"] == live.attrs["kline_src"] == RAW
    assert bt.attrs["kline_dim"] == live.attrs["kline_dim"] == "raw"

    merged = bt[BASE_COLS].merge(live[BASE_COLS], on="dt",
                                 suffixes=("_bt", "_live"))
    assert len(merged) == len(rows), "回测的桶必须全部出现在实盘读数里"
    for col in ("open", "high", "low", "close"):
        assert (merged[f"{col}_bt"].to_numpy()
                == merged[f"{col}_live"].to_numpy()).all(), f"{col} 同桶不等"

    # 两次调用都只能打在同一条 SQL 上
    fetch_sql = [s for s, _ in sess.calls if "adj_offset" in s]
    assert len(fetch_sql) == 2
    assert all(_sql_single_source(s) for s in fetch_sql)


# ---------------------------------------------------------------------------
# 2) 口径常量与废弃源
# ---------------------------------------------------------------------------
def test_single_source_constants_and_dimensions():
    assert fk.RAW_TABLE == RAW
    assert fk.BA_TABLE == BA
    assert fk.normalize_src("bar_60m") == "raw"
    assert fk.normalize_src("continuous") == "raw"
    assert fk.normalize_src("bar_60m_ba") == "back_adj"
    assert fk.normalize_src("back_adj") == "back_adj"
    assert fk.FREQ_SEG == "min60"


@pytest.mark.parametrize("bad", ["akshare", "csv", "agg_1m", "hourly_bar", ""])
def test_deprecated_independent_sources_rejected(bad):
    """P0-3 铁律：独立采集链不得被「兼容着读」。"""
    with pytest.raises(ValueError, match="不是允许的 K 线维度"):
        fk.normalize_src(bad)


def test_unknown_context_rejected():
    sess = _FakeSession(_bars(5))
    with pytest.raises(ValueError, match="未知 context"):
        fk.read_fusion_kline(sess, "RB888", None, src="bar_60m", context="prod")


# ---------------------------------------------------------------------------
# 3) 反向哨兵：真源被改回独立采集链时必须测得出
# ---------------------------------------------------------------------------
def test_source_drift_sentinel_fires(monkeypatch):
    sess = _FakeSession(_bars(20))
    monkeypatch.setattr(fk, "RAW_TABLE", FORBIDDEN)
    fk.read_fusion_kline(sess, "RB888", None, src="bar_60m", context="backtest")
    sqls = [s for s, _ in sess.calls]
    assert any(FORBIDDEN in s and RAW not in s for s in sqls), \
        "哨兵前提不成立：RAW_TABLE 改动没有反映到 SQL"
    assert not _sql_single_source(sqls[-1]), \
        "失效模式：真源被换成 l0_raw.hourly_bar 后白名单仍判通过"


# ---------------------------------------------------------------------------
# 4) 接线 AST：两条链路必须调用同一入口（防「各写一套」）
# ---------------------------------------------------------------------------
def test_both_chains_wired_to_single_entry():
    _, bt_fn = _func_node("app/backtest/fusion_backtest.py", "_read_hourly")
    assert "read_fusion_kline" in _call_names(bt_fn), \
        "回测侧不再调用唯一取数入口"
    bt_src = ast.get_source_segment(
        (REPO_ROOT / "app/backtest/fusion_backtest.py").read_text(encoding="utf-8"),
        bt_fn) or ""
    assert 'context="backtest"' in bt_src

    _, live_fn = _func_node("app/strategies/fusion_signal.py", "_read_src")
    assert "read_fusion_kline" in _call_names(live_fn), \
        "实盘侧不再调用唯一取数入口"
    live_src = ast.get_source_segment(
        (REPO_ROOT / "app/strategies/fusion_signal.py").read_text(encoding="utf-8"),
        live_fn) or ""
    assert 'context="live"' in live_src


def test_walkforward_range_has_no_hourlybar():
    """E2 / CL-20261108-13 防回退：日期范围也必须来自唯一真源。"""
    _, fn = _func_node("app/backtest/walkforward.py", "_data_range")
    assert "fusion_kline_range" in _call_names(fn)
    assert not _has_name(fn, "HourlyBar"), \
        "walkforward 又读回 HourlyBar（第二取数路径）"


def test_fusion_kline_range_is_same_source():
    sess = _FakeSession(_bars())
    lo, hi = fk.fusion_kline_range(sess, ["RB888"], "bar_60m")
    assert (lo, hi) == (date(2026, 1, 2), _bars()[-1][0].date())
    sql = sess.calls[-1][0]
    assert RAW in sql and FORBIDDEN not in sql
    assert fk.fusion_kline_range(sess, [], "bar_60m") == (None, None)
    with pytest.raises(ValueError):
        fk.fusion_kline_range(sess, ["RB888"], "akshare")
