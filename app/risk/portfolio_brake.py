# -*- coding: utf-8 -*-
"""V5 · 组合级回撤熔断（执行层风控标量）。

来源：《PRD_借鉴落地验证.md》V5 + 《架构设计_CB执行版.md》T20（D2 阈值 15% / 25%）。

定位（架构铁律）
----------------
* **不动** `walk_fusion_states` 状态机 —— 本模块只在其**下游执行层**叠加一个
  全局标量，缩放"期望手数"。故不触发 Qi Analisy skill 双源同步（该铁律只约束
  状态机真源）。
* 与因子层的 `position_cap_scalar` **乘法合成**，互不干扰。
* `enabled=false`（默认）时 `scalar ≡ 1.0`，与"从未上线"逐位等价（T9 回归保证）。

阈值（D2 初设，可由历史净值回放反推微调）
----------------------------------------
    回撤 > 15% → scalar = 0.5（降半仓）
    回撤 > 25% → scalar = 0.0（清仓观望）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class BrakeConfig:
    enabled: bool = False      # 默认关闭 = 旧行为
    dd_warn: float = 0.15      # 回撤 > 15% → 半仓
    dd_stop: float = 0.25      # 回撤 > 25% → 清仓
    lookback: int = 250        # 峰值回看窗口（交易日）


def load_brake_config() -> BrakeConfig:
    """从 cfg_feature_switch（迁移 008）读取熔断配置；表缺失/异常时退回默认（关闭）。

    调用方（scheduler._apply_portfolio_brake）改用本函数即可由数据库控制启停，
    无需改 env / 改代码。enabled 与阈值 dd_warn/dd_stop/lookback 均可经库内
    UPDATE cfg_feature_switch 调整（value 列存 JSON，形如 {"dd_warn":0.15,...}）。
    """
    from app.core.feature_switch import is_enabled, get_value

    enabled = is_enabled("portfolio_brake_enabled", default=False)
    cfg = BrakeConfig(enabled=enabled)
    raw = get_value("portfolio_brake_enabled")
    if raw:
        try:
            import json
            kv = json.loads(raw) if raw.strip().startswith("{") else {}
            cfg.dd_warn = float(kv.get("dd_warn", cfg.dd_warn))
            cfg.dd_stop = float(kv.get("dd_stop", cfg.dd_stop))
            cfg.lookback = int(kv.get("lookback", cfg.lookback))
        except (ValueError, TypeError):
            logger.warning("[portfolio_brake] value 解析失败，用默认值")
    return cfg


def max_drawdown(equity: list[float]) -> float:
    """权益序列的最大回撤（正数，如 0.18 表示 -18%）。"""
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            d = (peak - v) / peak
            if d > mdd:
                mdd = d
    return mdd


def current_drawdown(equity: list[float], lookback: int = 250) -> float:
    """当前回撤（相对回看窗口内的峰值）。"""
    if not equity:
        return 0.0
    win = equity[-lookback:] if lookback > 0 else equity
    peak = max(win)
    if peak <= 0:
        return 0.0
    return (peak - win[-1]) / peak


def brake_scalar(equity: list[float], cfg: BrakeConfig) -> float:
    """按当前回撤计算组合刹车标量。

    Returns:
        1.0（不动）/ 0.5（半仓）/ 0.0（清仓）。cfg.enabled=False 时恒为 1.0。
    """
    if not cfg.enabled or not equity:
        return 1.0
    dd = current_drawdown(equity, cfg.lookback)
    if dd > cfg.dd_stop:
        logger.warning(f"[portfolio_brake] 回撤 {dd:.2%} > {cfg.dd_stop:.0%} → 清仓观望")
        return 0.0
    if dd > cfg.dd_warn:
        logger.warning(f"[portfolio_brake] 回撤 {dd:.2%} > {cfg.dd_warn:.0%} → 降半仓")
        return 0.5
    return 1.0


def apply(expected_lots: float, equity: list[float], cfg: BrakeConfig) -> float:
    """把刹车标量应用到期望手数（执行层唯一入口）。

    enabled=False 时返回原值（恒等变换，双向等价性由 T9 回归保证）。
    """
    s = brake_scalar(equity, cfg)
    if s >= 1.0:
        return expected_lots
    return expected_lots * s


def load_equity_curve(session, symbol: str | None = None,
                      lookback: int = 250) -> list[float]:
    """从已平仓盈亏派生组合权益曲线（单位：元，起点 0 的累计盈亏）。

    数据源优先级：
      1. `fusion_position` 的已实现盈亏（若存在该字段）
      2. 回退：按 `fusion_state_detail` 的持仓状态 × 收盘价变动近似

    说明：当前库内尚无独立 `portfolio_equity` 表。若后续新增该轻表，
    只需替换本函数实现，上层调用不变（这正是分层的好处）。
    """
    from sqlalchemy import text

    try:
        # 优先取 equity（累计权益，口径正确）；没有则用 realized_pnl 累加成权益曲线。
        # ⚠ 不能直接用 realized_pnl 序列算回撤：那是**单日盈亏**，
        #    其峰值是"赚最多的那一天"，算出来的是假回撤。
        rows = session.execute(text(
            "SELECT trade_date, equity, realized_pnl FROM portfolio_equity "
            "ORDER BY trade_date DESC LIMIT :n"), {"n": lookback}).fetchall()
        rows = rows[::-1]
        eq = [float(r[1]) for r in rows if r[1] is not None]
        if eq:
            return eq
        pnl = [float(r[2]) for r in rows if r[2] is not None]
        if pnl:
            cum, acc = [], 0.0
            for v in pnl:
                acc += v
                cum.append(acc)
            return cum
    except Exception:  # noqa: BLE001  表不存在或字段缺失 → 走回退
        session.rollback()

    session.rollback()
    logger.warning("[portfolio_brake] portfolio_equity 不可用，返回空权益曲线"
                   "（scalar 恒为 1.0，等价关闭）")
    return []
