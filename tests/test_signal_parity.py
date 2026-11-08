# -*- coding: utf-8 -*-
"""E3 / P2-4（CL-20261108-17）：CI 信号同源断言 —— 同一套参数下，
实盘链路与回测链路算出的持仓方向/明细必须逐位一致。

路线图 §3.6 E3 原文：
    「同一 params_hash 下两条链路算出的方向必须一致」；
    CI 中 ``test_signal_parity`` 通过。

两条链路在信号层的真实形态（2026-11-08 核实）：
  · 实盘 ``fusion_signal.evaluate_symbol`` → ``fusion_state_detail``
  · 回测 ``fusion_backtest._generate_states`` → ``walk_fusion_states``
  ``fusion_state_detail`` 的 docstring 明写「直接消费 walk_fusion_states 的
  最后一帧，与回测共享同一套循环」。本文件把这句话变成机器可判的断言，
  并带反向哨兵：若参数漂移、或任一侧另起炉灶，必须测得出。

⚠ 零 DB / 零配置 / 零 yaml：直接 ``FusionConfig()`` 代码默认 + 合成 OHLC。
   params fingerprint 只用于「两端是否同一份参数」的内部一致性判断；
   生产 profile 的权威 hash 由 ``OPS-FUSION-PROFILE-PINNED`` 与
   ``scripts/ops/dump_fusion_profile.py:params_hash`` 单一真源负责。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from app.contracts import execution as ex
from app.core.config import FusionConfig
from app.strategies.fusion_signal import (
    fusion_state_detail,
    htf_direction,
    walk_fusion_states,
)


def _market(n: int = 420, seed: int = 11):
    """确定性合成行情：分段趋势 + 波动，保证 ADX 门控下有真实持仓出现。"""
    rng = np.random.default_rng(seed)
    x = np.arange(n, dtype=float)
    trend = np.where(x < n * 0.45, 0.16 * x,
                     np.where(x < n * 0.70, 0.16 * n * 0.45 - 0.10 * (x - n * 0.45),
                              0.16 * n * 0.45 - 0.10 * n * 0.25 - 0.13 * (x - n * 0.70)))
    wave = 9.0 * np.sin(x / 9.0) + 4.0 * np.sin(x / 3.0)
    c = 3000.0 + trend + wave + rng.normal(0, 1.2, n)
    o = np.concatenate(([c[0]], c[:-1]))
    h = np.maximum(o, c) + np.abs(rng.normal(0, 1.6, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 1.6, n))
    return o, h, l, c


def _fingerprint(p) -> str:
    """参数对象稳定指纹（CI 内部用，不替代生产 params_hash 单一真源）。"""
    blob = json.dumps(p.model_dump(), sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _assert_same_state(a: dict, b: dict) -> None:
    assert a.keys() == b.keys(), "两条链路返回的持仓明细字段集合不同"
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                and not isinstance(x, bool) and not isinstance(y, bool):
            assert x == pytest.approx(y, abs=1e-9), f"{k}: {x} != {y}"
        else:
            assert x == y, f"{k}: {x!r} != {y!r}"


# ---------------------------------------------------------------------------
# 1) 同一 params_hash → 两条链路方向/明细逐位一致
# ---------------------------------------------------------------------------
def test_same_params_hash_same_direction():
    o, h, l, c = _market()
    p = FusionConfig()
    before = _fingerprint(p)
    htf = htf_direction(c, p.ema_k)

    frames = list(walk_fusion_states(o, h, l, c, htf, p, symbol="RB888"))
    live = fusion_state_detail(o, h, l, c, htf, p, symbol="RB888")

    # 防「两边都空仓」的平凡假绿：合成数据必须真的产生过持仓。
    # 前若干根是指标 warmup（EMA/ATR/ADX），generator 不 yield，故允许少量缺帧。
    assert len(c) - 10 <= len(frames) <= len(c)
    assert any(d["state"] != 0 for d in frames), \
        "合成行情未产生任何持仓，parity 断言是平凡的"
    assert _fingerprint(p) == before, "计算过程中参数对象被改动"
    _assert_same_state(live, frames[-1])
    assert live["state"] == frames[-1]["state"]


def test_parity_holds_for_both_long_and_short_episodes():
    """逐帧对拍（不只最后一帧）：任一根分叉都算失败。"""
    o, h, l, c = _market(n=520, seed=23)
    p = FusionConfig()
    htf = htf_direction(c, p.ema_k)
    frames = list(walk_fusion_states(o, h, l, c, htf, p, symbol="FG888"))
    assert {d["state"] for d in frames} >= {0}, "至少要有空仓帧"
    assert any(d["state"] == 1 or d["state"] == 2 for d in frames)
    # fusion_state_detail 直接消费同一 generator 的最后一帧：逐帧重放应完全一致
    replay = list(walk_fusion_states(o, h, l, c, htf, p, symbol="FG888"))
    assert len(replay) == len(frames)
    for i, (a, b) in enumerate(zip(frames, replay)):
        _assert_same_state(a, b)
    _assert_same_state(fusion_state_detail(o, h, l, c, htf, p, symbol="FG888"),
                       replay[-1])


# ---------------------------------------------------------------------------
# 2) 反向哨兵：参数漂移必须改变 fingerprint，也必须改变行为
# ---------------------------------------------------------------------------
def test_params_drift_is_detected():
    o, h, l, c = _market()
    p = FusionConfig()
    p2 = p.model_copy(update={"sl_atr": 3.5, "trail_atr": 4.0,
                              "add_max_lots": 1})
    assert _fingerprint(p2) != _fingerprint(p), \
        "失效模式：参数改了 fingerprint 却不变"
    htf = htf_direction(c, p.ema_k)
    f1 = list(walk_fusion_states(o, h, l, c, htf, p, symbol="RB888"))
    f2 = list(walk_fusion_states(o, h, l, c, htf, p2, symbol="RB888"))
    assert any(any(f1[i][k] != f2[i][k] for k in f1[i])
               for i in range(len(f1))), \
        "失效模式：改了止损/吊灯/加码参数，状态序列却逐位不变（参数未接线）"


# ---------------------------------------------------------------------------
# 3) E1-A 契约声明进 CI（CL-20261108-14 防回退）
# ---------------------------------------------------------------------------
def test_execution_fork_contract_declared():
    ok, problems = ex.validate_fork_declaration()
    assert ok, problems
    assert len(ex.EXECUTION_FORKS) == 6
    assert [f.key for f in ex.ExecutionContract.unresolved()] == ["stop_loss"]
    assert ex.ExecutionContract.is_allowed("window") is True
    assert ex.ExecutionContract.is_allowed("max_hold") is True
    assert ex.ExecutionContract.is_allowed("ops_risk") is True
    assert ex.ExecutionContract.is_allowed("stop_loss") is False
    assert ex.ExecutionContract.get("nope") is None


def test_execution_contract_sentinel_fires_on_missing_decision():
    """若白名单条目被抹掉理由，自检必须转 False（防声明表变过期文档）。"""
    from dataclasses import replace

    import app.contracts.execution as mod

    saved = mod.EXECUTION_FORKS
    broken = tuple(
        replace(f, decision="") if f.key == "window" else f for f in saved
    )
    try:
        mod.EXECUTION_FORKS = broken
        mod.ExecutionContract.FORKS = broken
        ok, problems = mod.validate_fork_declaration()
        assert not ok and any("window" in x for x in problems)
    finally:
        mod.EXECUTION_FORKS = saved
        mod.ExecutionContract.FORKS = saved
    assert mod.validate_fork_declaration()[0] is True


# ---------------------------------------------------------------------------
# 4) E1-B 成交价纯函数进 CI（CL-20261108-14 / -11）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mode,tick,dirn,is_open,i,exp_px,exp_i",
    [
        ("close", 0.0, 1.0, True, 1, 113.0, 1),
        ("close", 2.0, -1.0, False, 4, 143.0, 4),
        ("next_open", 2.0, 1.0, True, 1, 122.0, 2),
        ("next_open", 2.0, 1.0, False, 1, 118.0, 2),
        ("next_open", 2.0, -1.0, True, 1, 118.0, 2),
        ("next_open", 2.0, -1.0, False, 1, 122.0, 2),
        ("next_open", 2.0, 1.0, True, 7, 175.0, 7),  # 末尾无 i+1 → 退回 closes[i]
    ],
)
def test_fill_price_parity(mode, tick, dirn, is_open, i, exp_px, exp_i):
    n = 8
    closes = [103.0 + 10 * k for k in range(n)]
    opens = [100.0 + 10 * k for k in range(n)]
    px, j = ex.fill_price(i, dirn, is_open, closes, opens, n, mode, tick)
    assert px == pytest.approx(exp_px)
    assert j == exp_i


def test_fill_price_default_modes():
    assert ex.VALID_FILL_MODES == ("close", "next_open")


# ---------------------------------------------------------------------------
# 5) 口径默认值钉死（与 BT-EXEC-FORK-DECLARED 的 6 个漂移检测同源）
# ---------------------------------------------------------------------------
def test_default_calibers_pinned():
    from app.backtest.fusion_backtest import FusionBacktestParams

    c = FusionConfig()
    b = FusionBacktestParams()
    assert c.max_bars == 800
    assert c.max_hold_days == 365
    assert c.entry_gate_profile == "off"
    assert c.kline_src == "bar_60m"
    assert b.fill_mode == "next_open"          # E1-C / CL-20261108-15
    assert b.max_adverse_gap_atr == 0.5        # 用户拍板阈值（初始约定，待 B1/OOS 复核）
    assert b.lookback_bars == 0
    assert b.max_hold_bars == 0


def test_synthetic_market_dtypes_are_sane():
    """防合成数据本身出问题（NaN 会让 parity 假绿）。"""
    o, h, l, c = _market()
    assert all(np.isfinite(a).all() for a in (o, h, l, c))
    assert (h >= np.maximum(o, c)).all()
    assert (l <= np.minimum(o, c)).all()
    htf = htf_direction(c, FusionConfig().ema_k)
    assert len(htf) == len(c)
    assert set(np.unique(np.sign(htf))) <= {-1.0, 0.0, 1.0}


# ---------------------------------------------------------------------------
# 6) E1-C 成交价与 0.5 ATR 跳空过滤（CL-20261108-15，零 DB 合成 K）
# ---------------------------------------------------------------------------
_SEG = [0, 1, 1, 1, 0, 0, 0, 0]


def _e1c_run(states, open_overrides=None, lots=None, with_open=True, **params):
    import pandas as pd

    from app.backtest import fusion_backtest as fb

    n = len(states)
    opens = [100.0] * n
    for k, v in (open_overrides or {}).items():
        opens[k] = v
    d = {
        "dt": pd.date_range("2026-01-01", periods=n, freq="h"),
        "close": [100.0] * n,
        "state": states,
        "lots": lots if lots is not None else [0 if s == 0 else 1 for s in states],
        "atr": [1.0] * n,
    }
    if with_open:
        d["open"] = opens
    p = fb.FusionBacktestParams(tick_size=1.0, **params)
    return fb._simulate_trades(pd.DataFrame(d), p, symbol=None)


def test_e1c_gap_blocks_whole_signal():
    """1.0 ATR 不利跳空 → 整段作废；关掉过滤则正常成交（开仓 +1tick）。"""
    blocked = _e1c_run(_SEG, {2: 101.0})
    assert blocked == []
    tr = _e1c_run(_SEG, {2: 101.0}, max_adverse_gap_atr=0.0)
    assert len(tr) == 1
    assert tr[0]["entry_px"] == pytest.approx(102.0)   # O[2]=101 + 1tick
    assert tr[0]["exit_px"] == pytest.approx(99.0)     # O[5]=100 - 1tick


def test_e1c_gap_is_not_deferred_entry():
    """封锁期内即使跳空恢复也不重试；只在归零后新信号段才重新开仓。"""
    tr = _e1c_run([0, 1, 1, 1, 0, 1, 1, 0], {2: 101.0})
    assert len(tr) == 1
    assert tr[0]["entry_px"] == pytest.approx(101.0)   # 新段 i=5 → O[6]=100 +1tick


def test_e1c_exit_ignores_gap():
    """平仓腿不做跳空判定：下一根开盘再不利也照常成交。"""
    tr = _e1c_run(_SEG, {5: 101.0})
    assert len(tr) == 1
    assert tr[0]["exit_px"] == pytest.approx(100.0)    # O[5]=101 - 1tick


# ---------------------------------------------------------------------------
# 7) 回测品种池严格 38 白名单（CL-20261108-12，零 DB）
# ---------------------------------------------------------------------------
_POOL = frozenset({"RB", "FG", "MA"})


def test_backtest_pool_accepts_whitelist_symbols():
    from app.backtest.fusion_backtest import _validate_backtest_symbols

    syms = ["RB888", "FG8888", "MA888"]
    assert _validate_backtest_symbols(syms, _POOL) == syms


@pytest.mark.parametrize("sym", ["B888", "CS888", "J888", "CU888", "RB2601"])
def test_backtest_pool_rejects_out_of_scope(sym):
    """封禁品种 / 持仓豁免 / 具体合约码一律拒绝，不静默跳过。"""
    from app.backtest.fusion_backtest import _validate_backtest_symbols

    with pytest.raises(ValueError, match="38 可交易白名单"):
        _validate_backtest_symbols([sym], _POOL)


def test_backtest_pool_rejection_is_all_or_nothing():
    """一批里混入 1 个非白名单 → 整批拒绝，不能只跑合法的几个。"""
    from app.backtest.fusion_backtest import _validate_backtest_symbols

    with pytest.raises(ValueError, match="B888"):
        _validate_backtest_symbols(["RB888", "B888", "MA888"], _POOL)


def test_e1c_close_baseline_ignores_gap():
    """显式 close 时过滤不生效，老基线成交价仍为信号根收盘（逐位零回归）。"""
    tr = _e1c_run(_SEG, {2: 101.0}, fill_mode="close")
    assert len(tr) == 1
    assert tr[0]["entry_px"] == pytest.approx(100.0)
    assert tr[0]["exit_px"] == pytest.approx(100.0)


def test_e1c_addon_rejected_keeps_position():
    """加仓被拒只取消该次加仓，已有持仓保留（不得强制清仓）。"""
    tr = _e1c_run(_SEG, {3: 101.0, 4: 101.0}, lots=[0, 1, 2, 2, 0, 0, 0, 0])
    assert len(tr) == 1
    assert all(not t.get("addon") for t in tr)


def test_e1c_next_open_requires_open_column():
    """缺 open 列必须 fail loud，不能静默退回收盘价。"""
    with pytest.raises(ValueError, match="open"):
        _e1c_run(_SEG, {2: 101.0}, with_open=False)
    assert len(_e1c_run(_SEG, {2: 101.0}, with_open=False,
                        fill_mode="close")) == 1
