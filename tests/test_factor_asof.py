# -*- coding: utf-8 -*-
"""因子层单元测试（因子接入 PRD §13 V-spec）。不依赖外部数据库。"""
import datetime as dt

import pandas as pd

from app.factor.asof import BiasConfig, FactorContext, asof_join, compute_bias_multipliers


def test_asof_join_lag_one_no_self_join():
    """lag_days=1 时，第3日发布的因子不能在第3日自连，只能在第4日生效。"""
    factor = pd.DataFrame({
        "trade_date": [dt.date(2026, 1, 1), dt.date(2026, 1, 3)],
        "symbol": ["RB888", "RB888"],
        "z": [0.1, 0.5],
    })
    cal = pd.DataFrame({"trade_date": [dt.date(2026, 1, d) for d in (1, 2, 3, 4, 5)]})
    out = asof_join(factor, cal, lag_days=1, value_cols=["z"])
    row3 = out[out["trade_date"] == pd.Timestamp("2026-01-03")].iloc[0]
    row4 = out[out["trade_date"] == pd.Timestamp("2026-01-04")].iloc[0]
    # 第3日只能取到第1日发布的 0.1，取不到第3日自己的 0.5
    assert row3["z"] == 0.1, "lag_days=1 时第3日不应自连到自身发布的 0.5"
    assert row4["z"] == 0.5, "第4日应取到第3日发布的 0.5"


def test_lookup_available_at_red_line():
    """因子只在 available_at <= trade_date 时可用（T+1 红线）。"""
    ctx = FactorContext(registry={})
    ctx._cache = {("basis_rate_z", "RB888"): [(dt.date(2026, 1, 5), 1.2)]}
    assert ctx.lookup("basis_rate_z", "RB888", dt.date(2026, 1, 4)) is None
    assert ctx.lookup("basis_rate_z", "RB888", dt.date(2026, 1, 5)) == 1.2
    assert ctx.lookup("basis_rate_z", "RB888", dt.date(2026, 1, 6)) == 1.2


def test_bias_missing_floor():
    """关键因子缺失 → 降级到最保守地板（§9 T7）。"""
    reg = {"basis_rate_z": {"default_weight": 0.1, "max_weight": 0.08}}
    out = compute_bias_multipliers(
        {"basis_rate_z": None}, reg, BiasConfig(degrade_missing="floor")
    )
    assert out["position_cap_scalar"] == 0.5
    assert out["entry_gate"] == -1.0


def test_bias_all_zero_neutral():
    """所有因子 z=0 → 偏置乘子中性（cap=1.0, gate=0.0）。"""
    reg = {"f1": {"default_weight": 0.1}}
    out = compute_bias_multipliers({"f1": 0.0}, reg)
    assert out["position_cap_scalar"] == 1.0
    assert out["entry_gate"] == 0.0


def test_bias_suppress_position_on_bearish():
    """一致看空因子（库存/仓单偏高=偏空）→ 合成偏空：position_cap_scalar 压到地板，entry_gate 转负。

    约定：default_weight 符号编码方向（负=偏空因子），z 为标准化原值（越高=该因子条件越强）。
    故库存/仓单处于高位 → z 为正，权重为负 → 合成贡献为负 → 偏空。
    """
    reg = {
        "inv": {"default_weight": -0.1},
        "wh": {"default_weight": -0.1},
    }
    out = compute_bias_multipliers({"inv": 1.5, "wh": 1.5}, reg)
    # 偏空合成 → position_cap_scalar 被抑制到 < 1.0（地板 0.5 仅在因子缺失时触发）
    assert out["position_cap_scalar"] < 1.0
    assert out["entry_gate"] < 0.0


def test_zscore_winsorize():
    import numpy as np
    from app.factor.asof import zscore
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 100.0])
    z = zscore(s)
    assert abs(z.mean()) < 1e-9
    assert z.std(ddof=0) > 0
    # 极端值(100)被 winsorize 到上分位，不应主导
    assert z.iloc[-1] < 3.0


def test_asof_join_is_datetime_unit_agnostic():
    """asof_join 对时基单位不敏感（2026-10-08 回归）。

    根因回顾：两侧 merge key 单位各自推断 —— `to_datetime(date 对象)` 给
    `[s]`，而 `[s] + Timedelta(days=n)` 被 Timedelta 单位（新版 pandas 为 `us`）
    升精度成 `[us]`；`merge_asof` 强制两侧 dtype 一致 → pandas>=2 直接抛
    `MergeError: incompatible merge keys [0] dtype('<M8[s]') and dtype('<M8[us]')`。

    旧 pandas 统一 `datetime64[ns]` 时恰好同型，故这是**升级后才暴露**的潜伏缺陷。
    本测试用三种输入形态（date 对象 / 字符串 / 已是 Timestamp 且单位为 ns/us/s）
    交叉验证，确保修法（派生列跟随日历列单位）真的与单位无关，
    而不是「碰巧在当前 pandas 版本上好了」。
    """
    import datetime as _dt

    days = [1, 2, 3, 4, 5]
    vals = {"a": 0.1, "b": 0.5}
    # 三种日历/因子形态，单位各异
    variants = [
        ([_dt.date(2026, 1, d) for d in days], [vals["a"], vals["b"]], "date 对象"),
        ([f"2026-01-{d:02d}" for d in days], [vals["a"], vals["b"]], "字符串"),
        ([pd.Timestamp(f"2026-01-{d:02d}").as_unit("ns") for d in days],
         [vals["a"], vals["b"]], "Timestamp[ns]"),
        ([pd.Timestamp(f"2026-01-{d:02d}").as_unit("us") for d in days],
         [vals["a"], vals["b"]], "Timestamp[us]"),
        ([pd.Timestamp(f"2026-01-{d:02d}").as_unit("s") for d in days],
         [vals["a"], vals["b"]], "Timestamp[s]"),
    ]
    for cal_dates, fvals, label in variants:
        factor = pd.DataFrame({
            "trade_date": cal_dates[::2][:2],   # 1日、3日
            "symbol": ["RB888", "RB888"],
            "z": fvals,
        })
        cal = pd.DataFrame({"trade_date": cal_dates})
        out = asof_join(factor, cal, lag_days=1, value_cols=["z"])
        row3 = out[out["trade_date"] == pd.Timestamp("2026-01-03")].iloc[0]
        row4 = out[out["trade_date"] == pd.Timestamp("2026-01-04")].iloc[0]
        assert row3["z"] == vals["a"], f"[{label}] 第3日不应自动生效到自身发布的值"
        assert row4["z"] == vals["b"], f"[{label}] 第4日应取到第3日发布的值"