# -*- coding: utf-8 -*-
"""2026-11-06 接线批次的契约测试（锁死"接线后的行为"，防回退）。

覆盖：
  1. ``load_brake_config`` —— 库内 JSON > env > 默认 三级取值，且
     「库内有行」时**不**被 env 翻案（enabled 口径与旧 ``_switch_on`` 等价）。
  2. ``registry.scan_candidates`` / ``unwired_keys`` —— 注册表驱动的主信号筛选。
  3. ``persistence`` 幂等撞键 —— 同键不同账户**不得**复用旧单（否则新账户丢单）。
"""
from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# 1) load_brake_config 的三级取值
# ---------------------------------------------------------------------------
def _fake_switch(monkeypatch, row):
    import app.risk.portfolio_brake as pb

    monkeypatch.setattr(pb, "_read_switch_row", lambda *a, **k: row)


def test_brake_cfg_env_fallback_when_no_db_row(monkeypatch):
    """库里**没有**这一行 → 走 env（保持既有部署口径，env 兜底不可省）。"""
    import app.risk.portfolio_brake as pb

    _fake_switch(monkeypatch, None)
    monkeypatch.setenv("PORTFOLIO_BRAKE_ENABLED", "1")
    monkeypatch.setenv("PORTFOLIO_BRAKE_DD_WARN", "0.11")
    monkeypatch.setenv("PORTFOLIO_BRAKE_DD_STOP", "0.22")
    monkeypatch.setenv("PORTFOLIO_BRAKE_LOOKBACK", "123")
    cfg = pb.load_brake_config(env_fallback=True)
    assert cfg.enabled is True
    assert cfg.dd_warn == pytest.approx(0.11)
    assert cfg.dd_stop == pytest.approx(0.22)
    assert cfg.lookback == 123


def test_brake_cfg_db_row_wins_over_env(monkeypatch):
    """库里**有**这一行 → 库内值优先（运维显式配置不被 env 翻案）。"""
    import app.risk.portfolio_brake as pb

    _fake_switch(monkeypatch, (True, '{"dd_warn":0.09,"dd_stop":0.33,"lookback":77}'))
    monkeypatch.setenv("PORTFOLIO_BRAKE_DD_WARN", "0.99")
    monkeypatch.setenv("PORTFOLIO_BRAKE_LOOKBACK", "999")
    cfg = pb.load_brake_config(env_fallback=True)
    assert cfg.enabled is True
    assert cfg.dd_warn == pytest.approx(0.09)
    assert cfg.dd_stop == pytest.approx(0.33)
    assert cfg.lookback == 77


def test_brake_cfg_db_row_disabled_not_overridden_by_env(monkeypatch):
    """库内明确 enabled=false → env 不得把它翻成 true（与旧 _switch_on 等价）。"""
    import app.risk.portfolio_brake as pb

    _fake_switch(monkeypatch, (False, None))
    monkeypatch.setenv("PORTFOLIO_BRAKE_ENABLED", "1")
    cfg = pb.load_brake_config(env_fallback=True)
    assert cfg.enabled is False


def test_brake_cfg_no_env_fallback_uses_defaults(monkeypatch):
    """env_fallback=False → 阈值走内置默认（库内未给时）。"""
    import app.risk.portfolio_brake as pb

    _fake_switch(monkeypatch, (True, None))
    monkeypatch.setenv("PORTFOLIO_BRAKE_DD_WARN", "0.11")
    cfg = pb.load_brake_config(env_fallback=False)
    assert cfg.dd_warn == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# 2) 注册表驱动
# ---------------------------------------------------------------------------
def test_scan_candidates_only_hourly_fusion():
    """主信号扫描只取小时线主周期策略；magic_axis(15m) 按设计不入。"""
    from app.strategies.registry import scan_candidates

    keys = [s.key for s in scan_candidates()]
    assert "fusion_v34" in keys
    assert "magic_axis" not in keys


def test_unwired_keys_lists_enabled_but_not_wired():
    """已启用但未接入信号链的策略必须能被点名（防静默空转）。"""
    from app.strategies.registry import REGISTRY, SIGNAL_WIRED_KEYS, StrategySpec, register, unwired_keys

    spec = StrategySpec(key="_probe_unwired", name="探针", freq="hourly", fn=None, enabled=True)
    register(spec)
    try:
        assert "_probe_unwired" in unwired_keys()
        assert "_probe_unwired" not in SIGNAL_WIRED_KEYS
    finally:
        REGISTRY.pop("_probe_unwired", None)
    assert "_probe_unwired" not in unwired_keys()


def test_params_for_empty_by_default():
    """默认不返回任何覆盖 → fusion 参数逐位不变。"""
    from app.strategies.registry import params_for

    assert params_for("fusion_v34") == {}


# ---------------------------------------------------------------------------
# 3) 幂等撞键
# ---------------------------------------------------------------------------
def test_idempotency_key_same_scope_is_idempotent():
    """同账户同通道同键 → 幂等命中（既有行为不变）。"""
    from app.execution.persistence import build_idempotency_key

    k1 = build_idempotency_key("SIG1", "RB2701", "OPEN", __import__("datetime").date(2026, 11, 6))
    k2 = build_idempotency_key("SIG1", "RB2701", "OPEN", __import__("datetime").date(2026, 11, 6))
    assert k1 == k2


def test_scoped_key_differs_by_account():
    """账户不同 → 作用域键不同（撞键时落库用），避免 UNIQUE 把第二个账户的单吞掉。"""
    from app.execution.persistence import _scoped_key

    a = _scoped_key("K", "SIM", "ACC_A")
    b = _scoped_key("K", "SIM", "ACC_B")
    assert a != b
    assert a.endswith("@SIM:ACC_A")
    assert b.endswith("@SIM:ACC_B")


def test_same_scope_compares_account_and_channel():
    """同账户同通道 → 判为真幂等；任一不同 → 判为撞键（不复用旧单）。"""
    from app.execution.persistence import _same_scope

    old = {"id": 1, "account": "ACC_A", "channel": "SIM"}
    assert _same_scope(old, {"account": "ACC_A", "channel": "SIM"}) is True
    assert _same_scope(old, {"account": None, "channel": "SIM"}) is False
    assert _same_scope(old, {"account": "ACC_A", "channel": "REAL"}) is False


# ---------------------------------------------------------------------------
# 4) basis_factors 接线（compute_factor_value.py 的 C 组）
# ---------------------------------------------------------------------------
def _load_cf():
    import importlib
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("compute_factor_value")


class _FakeCur:
    """只回放预置行集的假 cursor（compute_c 只需 execute + fetchall）。"""

    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


def _spot_rows():
    import datetime as dt

    rows = []
    base = dt.date(2026, 9, 1)
    slopes = {"RB888": 0.001, "CU888": -0.0005, "M888": 0.0002}
    for sym, k in slopes.items():
        for i in range(40):
            d = base + dt.timedelta(days=i)
            rows.append((d, sym, 0.01 + i * k, 100.0 + i, 95.0 + i))
    return rows


def test_compute_c_produces_two_basis_factors():
    """C 组必须真的产出两个因子（**行数非 0** —— 跑过不等于取到数据）。"""
    mod = _load_cf()
    out = mod.compute_c(_FakeCur(_spot_rows()), "2026-09-01")
    fids = {r[0] for r in out}
    assert fids == {"f_basis_mean60_z", "f_basis_chg5_z"}
    # 3 品种 × (40 天 - 前 5 天) = 105 个点
    assert len(out) == 3 * 35 * 2, len(out)
    # z 必须有限（横截面 std>0），不能是 NaN 混过去
    assert all(r[4] is not None and r[4] == r[4] for r in out)


def test_compute_c_matches_basis_factors_semantics():
    """C 组的 raw 必须与 basis_factors 的口径逐位一致（真的调用了它）。"""
    mod = _load_cf()
    from app.features.position_factors import basis_factors

    rows = _spot_rows()
    out = mod.compute_c(_FakeCur(rows), "2026-09-01")
    rb = sorted([r for r in out if r[2] == "RB888" and r[0] == "f_basis_chg5_z"],
                key=lambda x: x[1])
    # 手工用 basis_factors 复算最后一天的 5 日变化
    import datetime as dt

    rb_src = [r for r in rows if r[1] == "RB888"]
    rb_src.sort(key=lambda x: x[0])
    win = rb_src[-6:]
    import pandas as pd

    df = pd.DataFrame(win, columns=["report_date", "symbol", "dom_basis_rate",
                                    "dom_basis", "near_basis"])
    expect = basis_factors(df)["basis_change_5d"]
    assert rb[-1][1] == dt.date(2026, 9, 1) + dt.timedelta(days=39)
    assert rb[-1][3] == pytest.approx(expect)


def test_compute_c_no_rows_returns_empty():
    """源表无数据 → 返回空列表（不静默产出垃圾行）。"""
    mod = _load_cf()
    assert mod.compute_c(_FakeCur([]), "2026-09-01") == []
