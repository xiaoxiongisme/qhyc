# -*- coding: utf-8 -*-
"""V5X.3 参数真源与冻结校验 — CL-20261108-22 / C1。

为什么参数要单独放一个文件
--------------------------
``V5XConfig`` 的默认值写在 ``engine.py`` 里（与参考引擎逐位一致），但「默认值
是否仍等于 2026-10-23 冻结的那一套」必须能被机器判定 —— 否则一次「顺手调个
止损」就会让所有历史对拍结论失效，而且不报错。

本模块把 ``v5x_frozen_params.json`` 的 20 项抄成一份独立常量：
  · ``FROZEN_V5X3``  —— 冻结值（第二真源，故意与 engine.py 重复）
  · ``check_defaults()`` —— 返回不一致项；守卫 ``V5X-ENGINE-PARITY`` 用它断
  · ``config_from(overrides)`` —— 以冻结值为基线叠加覆盖

⚠ 用户 2026-11-08：「后期策略我会再调整」。调整应当**通过 overrides 显式传入**
  （进回测报告、可追溯），而不是改 ``V5XConfig`` 的默认值 —— 改默认值等于
  「悄悄换了策略」，回测数字与历史不可比。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.strategies.v5x.engine import V5XConfig

__all__ = [
    "FROZEN_V5X3",
    "FROZEN_ONLY_KEYS",
    "config_from",
    "check_defaults",
    "describe",
]

#: V5X.3 冻结参数（2026-10-23），逐项抄自
#: ``E:/抖音分析/2026-10-16-19-50-44/kline_encoding/v5x_frozen_params.json``
#: 的 ``params`` 段（20 项，按 key 排序）。
FROZEN_V5X3: dict[str, Any] = {
    "ADD_MAX_LOTS": 2,
    "ADD_THR": 1.5,
    "ADX_MIN": 15.0,
    "ALT_GATE": "m5",
    "ALT_N": 20,
    "ALT_THR": 1.0,
    "ATR_N": 14,
    "BE_R": 1.0,
    "BK_N": 10,
    "COOLDOWN": 7,
    "EMA_DIR": 140,
    "FIB_TOL": 0.5,
    "M5_Z_THR": 1.0,
    "MA_N": 20,
    "REGIME_P2_AUTO": True,
    "REGIME_P2_VOLZ": 2.5,
    "REGIME_POS_SCALE": False,
    "SL_ATR": 1.75,
    "TRAIL_ATR": 2.0,
    "W_PIVOT": 60,
}

#: 只在上表之外、但会影响行为的字段：它们的默认值由引擎原文决定，
#: 不在冻结 json 里，故单独列出来提醒「改这些同样是改策略」。
FROZEN_ONLY_KEYS: tuple[str, ...] = (
    "M5_ON",             # 会员净持仓门总开关
    "M1A_ON",            # MACD 柱同号门总开关
    "ENTRY_MIN_I",       # 预热根数
    "FIB_RATIOS",        # 斐波那契回撤位
    "HVOL_Q",            # 高波日分位（hvol 规则用）
    "CLUSTER_SHOCK_K",   # 簇冲击阈值
    "TP_R",              # 固定 R 止盈（None=关）
)


def config_from(overrides: dict[str, Any] | None = None) -> V5XConfig:
    """以冻结值为基线构造配置，再叠加 ``overrides``。

    未在 ``overrides`` 出现的字段一律取 ``V5XConfig`` 默认值（= 冻结值）。
    传入未知字段直接抛错：静默忽略等于「以为调了其实没调」。
    """
    cfg = V5XConfig()
    if not overrides:
        return cfg
    valid = set(asdict(cfg))
    unknown = sorted(set(overrides) - valid)
    if unknown:
        raise ValueError(
            f"未知 V5X 参数 {unknown}；可用字段见 V5XConfig（共 {len(valid)} 项）")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def check_defaults() -> list[tuple[str, Any, Any]]:
    """返回 ``(字段, 冻结值, 当前默认值)`` 的不一致项；空列表 = 一致。"""
    cfg = asdict(V5XConfig())
    bad: list[tuple[str, Any, Any]] = []
    for k, v in FROZEN_V5X3.items():
        cur = cfg.get(k)
        if isinstance(v, float) and isinstance(cur, (int, float)):
            if abs(float(cur) - v) > 1e-12:
                bad.append((k, v, cur))
        elif cur != v:
            bad.append((k, v, cur))
    return bad


def describe(cfg: V5XConfig | None = None) -> dict[str, Any]:
    """把配置摊平成 dict（进回测报告 / 信号流水，便于事后追溯用的是哪套）。"""
    return asdict(cfg or V5XConfig())
