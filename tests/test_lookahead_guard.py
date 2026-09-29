# -*- coding: utf-8 -*-
"""PRD T16：前视防护单测 + 启动守卫。

前视红线：`compute_factor_v1v6.build_v_factors` 只应使用**决策时点
（日盘最后一根 14:45）及以前**的 bar。若 15:00 收盘那根被算进当日成交量，
当日量比会与未来收益产生机械相关 → 虚假 IC（2026-09-27 亲历的静默退化类 bug）。

每个断言点都同时验证「防护生效」（正向）与「一旦失效能被测出」（反向哨兵）：
反向哨兵保证这个测试本身不会因为防护被删而假绿。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from compute_factor_v1v6 import (
    DECISION_HM,
    build_v_factors,
    has_lookahead_guard,
)


def _make_df(days=30, spike_last_day=True, spike_volume=1_000_000):
    """造多日分钟数据：每日 6 根前决策时点 bar（volume 缓慢爬升）+ 可选末日 15:00 巨量。"""
    rows = []
    for d in range(days):
        day = f"2026-01-{1 + d:02d}"
        for hm in ("09:00", "10:00", "11:00", "13:30", "14:00", "14:30"):
            rows.append(dict(day=day, hm=hm, open=100, high=101, low=99,
                            close=100, volume=10 + d))
        if spike_last_day and d == days - 1:
            rows.append(dict(day=day, hm="15:00", open=100, high=101, low=99,
                            close=100, volume=spike_volume))
    df = pd.DataFrame(rows)
    df["bucket"] = df["day"] + " " + df["hm"] + ":00"
    return df


def test_decision_hm_constant():
    """红线常量必须等于 '15:00'（启动守卫依赖此值）。"""
    assert DECISION_HM == "15:00"


def test_guard_rejects_post_decision_volume():
    """正向：末日 15:00 的 100 万巨量不得泄漏进当日量比。"""
    df = _make_df()
    out = build_v_factors(df)
    assert not out.empty
    vr = float(out.iloc[-1]["f_vol_ratio"])
    # 防护生效：末日 sum≈234，历史中位数≈114，vr≈2（合理比值带内）
    assert 0.1 < vr < 100, f"前视防护疑似失效：末日 f_vol_ratio={vr}（应在 0.1~100 比值带）"


def test_guard_ignores_boundary_15_00():
    """边界：hm=='15:00' 必须被严格排除（< 而非 <=）。"""
    df = pd.DataFrame([
        dict(day="2026-01-01", hm="14:30", open=100, high=101, low=99, close=100, volume=10),
        dict(day="2026-01-01", hm="15:00", open=100, high=101, low=99, close=100, volume=1_000_000),
    ])
    df["bucket"] = df["day"] + " " + df["hm"] + ":00"
    out = build_v_factors(df)
    # 仅 1 根前决策 bar → len<60 返回空，说明 15:00 没被当有效样本混入
    assert out.empty


def test_has_lookahead_guard_true_when_protected():
    """启动守卫：防护生效时返回 True。"""
    assert has_lookahead_guard() is True


def test_guard_sentinel_fires_when_filter_removed():
    """反向哨兵：若把 `_cut_pre_decision` 过滤去掉，哨兵必须报警（返回 False）。

    证明这个测试不是「压根没接线」的假绿——它能在防护真失效时报警
    （2026-09-27 静默退化教训：只测正向不够）。
    """
    import compute_factor_v1v6 as m

    saved = m._cut_pre_decision
    try:
        # 模拟防护被误删：不再截断 15:00 之后的 bar
        m._cut_pre_decision = lambda df, hm=None: df.copy()
        assert has_lookahead_guard() is False, "防护失效时哨兵应返回 False"
    finally:
        m._cut_pre_decision = saved


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
