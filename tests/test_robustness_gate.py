# -*- coding: utf-8 -*-
"""稳健性闸门验收（把孤儿模块 robustness 真正接进系统的同时，锁死口径）。

为什么必须有这个测试
--------------------
`app/backtest/robustness.py` 长期是 0 引用孤儿。接线时踩到一个坑：融合引擎
（`fusion_backtest._simulate_trades`）产出的逐笔是 entry_dt/entry_px/pnl 口径，
**没有 R 和 risk**（ATR 风险未外露），直接喂给 `run_six_checks` 只会抛 KeyError ——
若调用方用 try/except 兜住，闸门会**假生效**（返回 {"error": ...} 却看着像在跑）。

所以这里锁两件事：
  1. 口径体检 `_missing_fields` 必须能拦住融合口径（防假生效回归）；
  2. `run_six_checks` 在原生口径（magic_axis.run_magic 输出）下能跑出完整判定。
"""
from __future__ import annotations

import math

import pytest
from fastapi import HTTPException

from app.api.routers.backtest import ROBUSTNESS_REQUIRED_FIELDS, _missing_fields
from app.backtest.robustness import run_six_checks, silent_degradation


def _native_trade(i: int, r: float, year: int = 2024) -> dict:
    """原生口径逐笔（与 magic_axis.run_magic 输出字段一致）。"""
    ep, risk = 3000.0 + i, 20.0
    dd = 1 if r >= 0 else -1
    # 保持 R = (xp - ep) * dir / risk 恒等 —— 否则对称性硬闸门会（正确地）判不一致
    xp = ep + abs(r) * risk
    return dict(
        sym=f"S{i % 5}", ei=i, xi=i + 3,
        edt=f"{year}-{1 + i % 12:02d}-01 09:30:00",
        xdt=f"{year}-{1 + i % 12:02d}-01 14:55:00",
        dir=dd, ep=ep, xp=xp,
        atr_e=10.0, risk=risk, R=r, mult=1.0, exit_kind="MAGIC",
    )


def _fusion_style_trade() -> dict:
    """融合引擎口径逐笔（缺 R/risk —— 必须被体检拦下）。"""
    return dict(
        symbol="RB.SHF", side="LONG",
        entry_dt="2024-03-01 10:00:00", exit_dt="2024-03-01 14:00:00",
        entry_px=3500.0, exit_px=3520.0, pnl=20.0, pnl_pct=0.57, addon=False,
    )


# --- 1) 口径体检 ------------------------------------------------------------

def test_missing_fields_on_empty():
    assert _missing_fields([]) == list(ROBUSTNESS_REQUIRED_FIELDS)


def test_missing_fields_accepts_native_schema():
    assert _missing_fields([_native_trade(0, 1.5)]) == []


def test_missing_fields_rejects_fusion_schema():
    """回归防线：融合口径缺 R/risk/dir，绝不能被当成可过闸的逐笔。"""
    miss = _missing_fields([_fusion_style_trade()])
    assert miss, "融合口径不应通过口径体检（缺 R/risk/dir）"
    assert {"R", "risk", "dir"} <= set(miss)


def test_endpoint_rejects_fusion_schema_with_400():
    """端到端：口径不对直接 400，而不是在 try/except 里变成假生效的 error 字段。"""
    from app.api.routers.backtest import RobustnessRequest, run_robustness

    with pytest.raises(HTTPException) as ei:
        run_robustness(RobustnessRequest(trades=[_fusion_style_trade()]))
    assert ei.value.status_code == 400
    assert "R" in str(ei.value.detail)


def test_endpoint_accepts_native_schema():
    from app.api.routers.backtest import RobustnessRequest, run_robustness

    res = run_robustness(
        RobustnessRequest(trades=[_native_trade(i, 1.2) for i in range(20)])
    )
    assert "pass_all" in res and "gates" in res


# --- 2) 闸门在原生口径下能跑出完整判定 --------------------------------------

def test_run_six_checks_returns_full_verdict():
    # 60 笔、多数盈利、跨两年 —— 让分半/逐年/自助都有样本
    trades = [_native_trade(i, 1.5 if i % 4 else -1.0,
                            year=2023 if i < 30 else 2025)
              for i in range(60)]
    res = run_six_checks(trades, cost_bp=5.0)

    for k in ("n_trades", "net", "pf", "mdd", "concentration", "split_half",
              "by_year", "cost", "mc", "bootstrap", "symmetry",
              "gates", "pass_all", "failed"):
        assert k in res, f"缺返回字段 {k}"
    assert res["n_trades"] == 60
    assert isinstance(res["pass_all"], bool)
    assert isinstance(res["failed"], list)
    # 硬闸门之一：价格取负对称性必须在原生口径下逐笔一致
    assert res["symmetry"]["pass_"] is True
    assert math.isfinite(res["net"]) and res["net"] > 0


def test_run_six_checks_without_base_has_no_silent_key():
    trades = [_native_trade(i, 1.2) for i in range(20)]
    res = run_six_checks(trades)
    assert "silent_degradation" not in res
    assert "非静默退化" not in res["gates"]


def test_silent_degradation_blocks_identical_count():
    """参数改了但笔数完全相同 ⇒ 判参数未生效（2026-09-27 亲历教训）。"""
    base = [_native_trade(i, 1.2) for i in range(20)]
    variant = [_native_trade(i, 1.2) for i in range(20)]   # 逐位相同
    out = run_six_checks(variant, base=base,
                         base_params={}, variant_params={"keyline_excl": True})
    assert out["n_trades"] == 20
    assert out["silent_degradation"]["silent"] is True
    assert "非静默退化" in out["gates"]
    assert out["gates"]["非静默退化"] is False
    assert out["pass_all"] is False   # 触发即阻断


def test_silent_degradation_ok_when_counts_differ():
    base = [_native_trade(i, 1.2) for i in range(20)]
    variant = [_native_trade(i, 1.2) for i in range(17)]
    s = silent_degradation(base, variant, {}, {"keyline_excl": True})
    assert s["silent"] is False
    assert s["verdict"] == "OK"
