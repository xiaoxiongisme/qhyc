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
import logging

logger = logging.getLogger(__name__)


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
    "v5x3": StrategySpec(
        key="v5x3",
        name="V5X.3（移植版）",
        freq="hourly",
        caliber="back_adj",
        fn=None,
        default_params={},
        enabled=True,
        note="2026-11-08 自 V5X 冻结版逐字移植（CL-20261108-22），仅具备"
             "**可复现回测 + 小时级信号推送**能力，**未接实盘下单/持仓同步**。"
             "不走 fusion 的 evaluate_all：由独立作业 v5x_scan 驱动"
             "（app/strategies/v5x/live.py），信号跑在 BA 后复权空间，"
             "出口反解回真实价。日频门控用 t-1 口径（gate_lag=1）。"
             "⚠ fn=None 是刻意的：V5X 引擎需要 DailyCtx，不是 fn(sym,data) 形状。",
    ),
}

#: 已接入**主信号链**的策略 key（2026-11-06 新增）。
#:
#: 为什么需要这张表：注册表里登记 ≠ 调度器会跑。历史上本模块只有
#: :func:`enabled_keys`（且**生产无调用方**），于是"在注册表里加一条策略"这件事
#: **没有任何后果** —— 又是一种「开关看似生效、实则静默空转」。本表把
#: "是否真的接进信号链"显式化，:func:`unwired_keys` 用它对账告警。
#:
#: 2026-11-08 补 ``v5x3``：它接的是**独立作业** ``v5x_scan``（:mod:`app.strategies.v5x.live`），
#: 不经过 ``fusion`` 的 ``evaluate_all``。所以"已接线"在这里的含义是
#: 「有确定的调度入口在跑它」，而不是「走融合那条链」——两条链互不影响。
SIGNAL_WIRED_KEYS: frozenset[str] = frozenset({"fusion_v34", "v5x3"})

#: 主信号扫描使用的主周期（小时线）—— 与 `settings.fusion` 的取数口径一致。
SCAN_FREQ = "hourly"


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


def enabled_keys(cfg: Optional[dict] = None) -> list[str]:
    """启用的策略 key 列表。

    若 config 中存在 `strategies` 段（形如 {key: {enabled: bool, params: {...}}}），
    以其覆盖注册表默认值 —— 便于"改配置即增减策略"，无需改代码。

    ``cfg`` 可显式传入（测试/非 settings 环境用）；不传则回落到 settings。
    显式注入是为了做 PRD T18「enabled=false ≡ 从未注册」的双向等价回归。
    """
    if cfg is None:
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("[silent-swallow] 异常被静默忽略（此处原本为 pass）: %s", exc, exc_info=True)
    return p


# ---------------------------------------------------------------------------
# ★ 2026-11-06 接线：让本模块真正参与生产调度（此前生产零调用方）
# ---------------------------------------------------------------------------

def scan_candidates(freq: str = SCAN_FREQ, cfg: Optional[dict] = None) -> list[StrategySpec]:
    """返回参与**主信号扫描**的策略（已启用 + 主周期匹配）。

    口径（与 :func:`enabled_keys` 严格一致）：
      · 「已启用」= 注册表 ``enabled`` 且未被 config 显式关闭；
      · 「主周期匹配」= ``spec.freq == freq``（主信号扫描走**小时线**）。

    ⚠ ``magic_axis``（freq=15m）按设计**独立于融合并跑、不叠加进融合状态机**
      （实测并联会稀释收益），故不在主信号扫描候选内 —— 这是**刻意**的，
      不是遗漏。若将来要接入，必须先过回测闸门。
    """
    keys = set(enabled_keys(cfg))
    return [s for s in all_specs() if s.freq == freq and s.key in keys]


def unwired_keys(cfg: Optional[dict] = None) -> list[str]:
    """已注册且已启用、但**未接入信号链**的策略 key（对账用）。

    这类策略"看起来在跑、实际不产生任何信号"——属本项目重点缺陷类型
    （开关看似生效、实则静默空转）。调用方应把它打进日志/告警。
    """
    return [k for k in enabled_keys(cfg) if k not in SIGNAL_WIRED_KEYS]
