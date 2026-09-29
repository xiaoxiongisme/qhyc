# -*- coding: utf-8 -*-
"""PRD T18：`enabled=false` 双向等价回归 ——「注册即中性」的硬保证。

为什么必须"双向"
----------------
只测**正向**（enabled=false ≡ 从未注册）是不够的：一个"压根没接线"的开关
同样能通过正向测试 —— 因为它从未被读取，自然与"不存在"完全一样。
这正是 2026-09-27 亲历的**静默退化**：新增开关型参数没被纳入惰性计算的触发
条件，系统不报错，只让参数"看起来生效实则无效"。

所以每个维度都测两个方向：
  * 正向：enabled=false  ≡  该条目从未注册（输出**逐位一致**）
  * 反向：enabled=true   ≠  从未注册（证明开关真的被读取）

不依赖数据库（`FactorContext.from_rows` 是纯函数构造）。
"""

from __future__ import annotations

import pytest

from app.factor.asof import (
    BiasConfig,
    FactorContext,
    compute_bias_multipliers,
    validate_max_weight,
)


# ---------------------------------------------------------------------------
# 造一行 factor_registry 的查询结果（与 from_rows 的 9 列顺序严格对应）
#   factor_id, name, category, data_sources, default_weight,
#   max_weight, horizon, lag_days, enabled
# ---------------------------------------------------------------------------

def _row(fid: str, enabled: bool, mw: float = 0.08, w: float = -0.1):
    return (fid, f"name_{fid}", "B", [], w, mw, "B", 1, enabled)


BASE = [_row("basis_rate_z", True, w=-0.1), _row("warehouse_receipt_z", True, w=-0.08)]


# ---------------------------------------------------------------------------
# 1. 因子层：registry 维度
# ---------------------------------------------------------------------------

def test_factor_disabled_equals_absent():
    """正向：注册但 enabled=false ≡ 从未注册（registry 逐位一致）。"""
    with_disabled = BASE + [_row("t18_probe", False)]
    r_absent = FactorContext.from_rows(BASE).registry
    r_disabled = FactorContext.from_rows(with_disabled).registry
    assert r_absent == r_disabled
    assert "t18_probe" not in r_disabled


def test_factor_enabled_is_included():
    """反向：enabled=true 必须出现 —— 证明 enabled 这一列真的被读。"""
    r_absent = FactorContext.from_rows(BASE).registry
    r_enabled = FactorContext.from_rows(BASE + [_row("t18_probe", True)]).registry
    assert "t18_probe" not in r_absent
    assert "t18_probe" in r_enabled
    assert r_absent != r_enabled


# ---------------------------------------------------------------------------
# 2. 因子层：端到端输出维度（偏置乘子）
# ---------------------------------------------------------------------------

def test_bias_multipliers_bitwise_identical():
    """正向：给禁用因子传很强的 z（1.7），合成乘子必须与"它不存在"逐位相同。"""
    z = {"basis_rate_z": 0.3, "warehouse_receipt_z": -0.2, "t18_probe": 1.7}
    out_absent = compute_bias_multipliers(z, FactorContext.from_rows(BASE).registry)
    out_disabled = compute_bias_multipliers(
        z, FactorContext.from_rows(BASE + [_row("t18_probe", False)]).registry)
    assert out_absent == out_disabled


def test_bias_multipliers_change_when_enabled():
    """反向：同一个 z，enabled=true 时合成结果必须不同（否则开关没接线）。"""
    z = {"basis_rate_z": 0.3, "warehouse_receipt_z": -0.2, "t18_probe": 1.7}
    out_absent = compute_bias_multipliers(z, FactorContext.from_rows(BASE).registry)
    out_enabled = compute_bias_multipliers(
        z, FactorContext.from_rows(BASE + [_row("t18_probe", True, w=0.2)]).registry)
    assert out_absent != out_enabled


def test_disabled_factor_missing_does_not_trigger_floor():
    """禁用因子即使 z=None 也不触发"缺失降级到地板"（与未注册一致）。"""
    cfg = BiasConfig(degrade_missing="floor")
    z = {"basis_rate_z": 0.3, "t18_probe": None}
    out_absent = compute_bias_multipliers(z, FactorContext.from_rows(BASE).registry, cfg)
    out_disabled = compute_bias_multipliers(
        z, FactorContext.from_rows(BASE + [_row("t18_probe", False)]).registry, cfg)
    assert out_absent == out_disabled
    assert out_disabled["position_cap_scalar"] != cfg.cap_floor

    # 反向：同一个因子启用后 z=None → 必须触发地板，证明降级路径也接线了
    out_enabled = compute_bias_multipliers(
        z, FactorContext.from_rows(BASE + [_row("t18_probe", True)]).registry, cfg)
    assert out_enabled["position_cap_scalar"] == cfg.cap_floor


# ---------------------------------------------------------------------------
# 3. 额度校验：退役因子不占额度
# ---------------------------------------------------------------------------

def test_validate_max_weight_ignores_disabled():
    """退役（enabled=false）但保留 max_weight 以便回滚的因子，不应占用额度。"""
    reg = FactorContext.from_rows(
        [_row("a", True, mw=0.9), _row("retired", False, mw=0.9)],
        enabled_only=False,
    ).registry
    assert validate_max_weight(reg) == []          # 只算启用的 0.9
    reg_all_enabled = dict(reg)
    reg_all_enabled["retired"] = dict(reg["retired"], enabled=True)
    assert validate_max_weight(reg_all_enabled) == ["a", "retired"]   # 1.8 > 1.0


# ---------------------------------------------------------------------------
# 4. 策略层：同一条双向性质
# ---------------------------------------------------------------------------

def test_strategy_disabled_equals_absent():
    """正向/反向：策略注册表同样满足 enabled=false ≡ 从未注册。"""
    from app.strategies.registry import REGISTRY, StrategySpec, enabled_keys, register

    before = sorted(enabled_keys(cfg={}))
    spec = StrategySpec(key="_t18_probe", name="T18 探针", freq="15m",
                        fn=None, enabled=False)
    register(spec)
    try:
        # 正向
        assert sorted(enabled_keys(cfg={})) == before
        # config 显式关闭同样等价于不启用
        spec.enabled = True
        assert sorted(enabled_keys(cfg={"_t18_probe": {"enabled": False}})) == before
        # 反向
        assert "_t18_probe" in enabled_keys(cfg={})
    finally:
        REGISTRY.pop("_t18_probe", None)
    assert sorted(enabled_keys(cfg={})) == before


def test_registry_invariants_untouched():
    """探针测试不能污染全局注册表（防止把上面的用例变成"改了全局状态"的假绿）。"""
    from app.strategies.registry import REGISTRY
    assert "_t18_probe" not in REGISTRY
    assert "fusion_v34" in REGISTRY


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
