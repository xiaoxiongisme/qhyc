# -*- coding: utf-8 -*-
"""策略 config 覆盖层必须真的生效（CL-20261108-09）。

历史问题：`YamlConfig` 里根本没有 `strategies` 字段，而 `registry.params_for`
用 `getattr(get_settings(), "strategies", None)` 取覆盖 —— 恒为 None。
于是 `scheduler._fusion_params` 的注释和告警文案（「只有 config 显式写了
`strategies.<key>.params` 才会覆盖」）与事实相反：照着注释改配置调参，静默无效。

这组用例钉死三件事：
  1. YAML 里的 `strategies` 段能被解析出来，并且 `Settings.strategies` 返回**纯 dict**
     （给 pydantic 模型会让 `isinstance(ov, dict)` 判假 → 覆盖再次静默失效）；
  2. `params_for` 真的合并了覆盖值；
  3. `_fusion_params` 真的把它落到 FusionConfig，且**未知字段丢弃**而不是静默吞掉。
"""
from __future__ import annotations

import pytest

from app.core.config import EnvSettings, Settings, YamlConfig


def _settings(strategies: dict | None = None) -> Settings:
    y = YamlConfig.model_validate({"strategies": strategies} if strategies else {})
    return Settings(env=EnvSettings(), yaml=y)


def test_settings_exposes_strategies_as_plain_dict():
    s = _settings({"fusion_v34": {"enabled": True, "params": {"sl_atr": 1.5}}})
    st = s.strategies
    assert isinstance(st, dict)
    assert isinstance(st["fusion_v34"], dict), "必须是原生 dict，否则 registry 会静默跳过"
    assert st["fusion_v34"]["params"] == {"sl_atr": 1.5}


def test_enabled_none_is_not_dumped():
    """``enabled: null`` 不得覆盖注册表默认值。"""
    s = _settings({"fusion_v34": {"enabled": None, "params": {}}})
    assert "enabled" not in s.strategies["fusion_v34"]


def test_params_for_merges_config_override(monkeypatch):
    import app.core.config as cfgmod
    from app.strategies.registry import params_for

    monkeypatch.setattr(cfgmod, "get_settings",
                        lambda: _settings({"fusion_v34": {"params": {"sl_atr": 1.5}}}))
    assert params_for("fusion_v34") == {"sl_atr": 1.5}


def test_params_for_empty_without_section(monkeypatch):
    import app.core.config as cfgmod
    from app.strategies.registry import params_for

    monkeypatch.setattr(cfgmod, "get_settings", lambda: _settings())
    assert params_for("fusion_v34") == {}, "无 strategies 段时必须是空覆盖（生产现状）"


def test_fusion_params_applies_override_and_drops_unknown(monkeypatch):
    """落到 FusionConfig：已知字段生效、未知字段丢弃、默认逐位不变。"""
    import app.core.config as cfgmod
    from app.scheduler import _fusion_params

    base = _settings().fusion
    monkeypatch.setattr(cfgmod, "get_settings", lambda: _settings(
        {"fusion_v34": {"params": {"sl_atr": 1.5, "no_such_field": 1}}}))
    got = _fusion_params(base, "fusion_v34")
    assert got.sl_atr == pytest.approx(1.5)
    assert not hasattr(got, "no_such_field")
    # 未覆盖的字段逐位不变
    assert got.trail_atr == base.trail_atr


def test_fusion_params_noop_without_override(monkeypatch):
    import app.core.config as cfgmod
    from app.scheduler import _fusion_params

    monkeypatch.setattr(cfgmod, "get_settings", lambda: _settings())
    base = _settings().fusion
    got = _fusion_params(base, "fusion_v34")
    assert got == base, "无覆盖时生效参数必须与代码默认逐位一致"


def test_enabled_override_can_disable_strategy(monkeypatch):
    import app.core.config as cfgmod
    from app.strategies.registry import enabled_keys

    monkeypatch.setattr(cfgmod, "get_settings", lambda: _settings(
        {"v5x3": {"enabled": False}}))
    assert "v5x3" not in enabled_keys()
