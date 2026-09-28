# -*- coding: utf-8 -*-
"""策略注册表（六层解耦 · 策略层）。

为什么要有它
------------
历史上策略是"硬编码进调度作业"的：fusion_scan 直接调 walk_fusion_states，
A 隐秘数轴只存在于 `runtime/bt_engine_min15.py` 这个调试脚本里。
要新增/停用/对比一个策略，必须改调度代码 —— 无法"方便调整和增减"。

注册表把策略变成**可声明的条目**：
    key → (取数周期, 口径, 纯函数, 默认参数, 是否启用)
上层（调度 / 回测 / 应用）只按 key 取用，新增策略只需 `register()` 一行。

约定
----
* 策略函数必须是**纯函数**：只吃数组/DataFrame，不碰数据库、不读时钟。
  （fusion_v34 的 walk_fusion_states 已满足；magic_axis 同风格）
* 默认**不改已有策略的一个字**（单一真源铁律）。
* 策略默认 enabled=False 的新策略须过回测闸门才允许开启（见 Phase 3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class StrategySpec:
    """一条策略声明。"""

    key: str
    name: str
    #: 主周期（对应 app/data/caliber.py FREQS）
    freq: str
    #: 取数口径（continuous = 回测基准）
    caliber: str = "continuous"
    #: 纯函数入口：fn(sym, data, **params) -> trades/state
    fn: Optional[Callable] = None
    default_params: dict = field(default_factory=dict)
    enabled: bool = True
    #: 说明与结论出处（便于回溯"为什么启用/否决"）
    note: str = ""


def _load():
    from app.strategies.fusion_signal import walk_fusion_states
    from app.strategies.magic_axis import run_magic
    return walk_fusion_states, run_magic


try:
    _walk_fusion_states, _run_magic = _load()
except Exception:  # noqa: BLE001  # 导入失败时不阻断（如依赖未就绪）
    _walk_fusion_states, _run_magic = None, None


REGISTRY: dict[str, StrategySpec] = {
    "fusion_v34": StrategySpec(
        key="fusion_v34",
        name="融合策略 V3.4",
        freq="hourly",
        caliber="continuous",
        fn=_walk_fusion_states,
        default_params={},
        enabled=True,
        note="单一真源。EMA140 方向 + ADX≥15 + 斐波汇流 + MA20 回踩 + 2×ATR 吊灯 "
             "+ 0.5×ATR 保本 + 阶梯加码 + 收盘判定。禁止修改逻辑本体；"
             "引擎与 Qi Analisy skill 须逐位一致。",
    ),
    "magic_axis": StrategySpec(
        key="magic_axis",
        name="A 隐秘数轴（日内区间回归）",
        freq="15m",
        caliber="continuous",
        fn=_run_magic,
        default_params={},
        enabled=True,
        note="与融合本质对立（逆势 vs 顺势），2026-09-26 实测并联会稀释收益"
             "（净¥ 18.09万 → 10.04万）。**独立于融合并跑，不叠加进融合状态机**。"
             "仅在 15m 成立；六项稳健性全过、容量梯度 3→10 仓完美单调。",
    ),
}


def register(spec: StrategySpec) -> None:
    """注册/覆盖一条策略（新增策略只需调用本函数）。"""
    REGISTRY[spec.key] = spec


def get(key: str) -> StrategySpec:
    """按 key 取策略声明，不存在则抛 KeyError（避免静默走偏）。"""
    if key not in REGISTRY:
        raise KeyError(f"未注册的策略 {key!r}；已注册：{sorted(REGISTRY)}")
    return REGISTRY[key]


def all_specs() -> list[StrategySpec]:
    return list(REGISTRY.values())


def enabled_keys() -> list[str]:
    """启用的策略 key 列表。

    若 config 中存在 `strategies` 段（形如 {key: {enabled: bool, params: {...}}}），
    以其覆盖注册表默认值 —— 便于"改配置即增减策略"，无需改代码。
    """
    try:
        from app.core.config import get_settings
        cfg = getattr(get_settings(), "strategies", None) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    out = []
    for k, spec in REGISTRY.items():
        ov = cfg.get(k) if isinstance(cfg, dict) else None
        if ov is not None and isinstance(ov, dict):
            if not ov.get("enabled", spec.enabled):
                continue
        elif not spec.enabled:
            continue
        out.append(k)
    return out


def params_for(key: str) -> dict:
    """取最终参数（注册表默认 + config 覆盖）。"""
    spec = get(key)
    p = dict(spec.default_params)
    try:
        from app.core.config import get_settings
        cfg = getattr(get_settings(), "strategies", None) or {}
        ov = cfg.get(key) if isinstance(cfg, dict) else None
        if isinstance(ov, dict) and isinstance(ov.get("params"), dict):
            p.update(ov["params"])
    except Exception:  # noqa: BLE001
        pass
    return p
