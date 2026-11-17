# -*- coding: utf-8 -*-
"""live-like 回测默认口径钉死实盘（CL-20261117-02）。

防的是一类静默退化：有人把 ab_v5x_backtest.py 的 argparse 默认值从
1/strict 改回 0/frozen，或把 src 换成复现口径 bar_60m，报告数字就回到
「含前视、与实盘不可比」，而没有任何东西报错。断言读的是**当前磁盘代码**，
所以这些用例同时验证「断言本身可解析到真值」和「真值与 live 常量一致」。
"""
from __future__ import annotations

from scripts.ops.guard_invariants import Ctx, _c_bt_live_like_profile


class _NoDB:
    """这条断言零 DB，传个占位 session 即可。"""


def test_live_like_profile_passes():
    ok, label, observed, detail = _c_bt_live_like_profile(Ctx(session=_NoDB()))
    assert ok, f"{label}: {observed} / {detail}"
    assert "live=bar_60m_ba/1/strict" in observed


def test_observed_reports_key_fields():
    ok, _, observed, _ = _c_bt_live_like_profile(Ctx(session=_NoDB()))
    assert "--gate-lag" in observed and "--regime-mode" in observed
    assert "src=" in observed and "fill=" in observed and "wired=True" in observed


def test_web_endpoint_profile_pinned():
    from scripts.ops.guard_invariants import _c_bt_v5x_endpoint_live

    ok, label, observed, detail = _c_bt_v5x_endpoint_live(Ctx(session=_NoDB()))
    assert ok, f"{label}: {observed} / {detail}"
    assert "gate_lag=1" in observed and "regime_mode='strict'" in observed


def test_web_endpoint_forbids_caliber_override():
    from app.api.routers.backtest import V5XBacktestRequest

    fields = set(getattr(V5XBacktestRequest, "model_fields", {}) or {})
    assert not ({"src", "gate_lag", "regime_mode", "fill_mode"} & fields), (
        f"请求模型不应暴露口径覆盖字段，实际有 {sorted(fields)}")


def test_fusion_is_lazy_imported_in_backtest_router():
    """CL-20261117-06：fusion 不得是 backtest 路由模块级加载前置条件。"""
    from scripts.ops.guard_invariants import _c_fusion_lazy_import

    ok, label, observed, detail = _c_fusion_lazy_import(Ctx(session=_NoDB()))
    assert ok, f"{label}: {observed} / {detail}"
    assert "top_fusion=0" in observed and "lazy_fns=" in observed
    assert "init_top_fusion=0" in observed


def test_v5x_endpoint_module_importable_without_fusion():
    """解耦实证：屏蔽 fusion_backtest 后，backtest 路由模块仍可独立 import。"""
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "app.backtest.fusion_backtest" or name.startswith("app.backtest.fusion_backtest."):
            raise ImportError("simulated fusion_backtest outage")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("app.api.routers.backtest", None)
    builtins.__import__ = blocking_import
    try:
        mod = importlib.import_module("app.api.routers.backtest")
        assert hasattr(mod, "run_v5x_live_like"), "V5X 端点应仍可加载"
        assert hasattr(mod, "V5X_LIVE_LIKE_PROFILE"), "V5X profile 应仍可加载"
    finally:
        builtins.__import__ = real_import
        sys.modules.pop("app.api.routers.backtest", None)
        if saved is not None:
            sys.modules["app.api.routers.backtest"] = saved
