# -*- coding: utf-8 -*-
"""因子层核心（因子接入 PRD 2026-09-26）。

提供：
- ``asof_join``：把因子值按「发布时点」对齐到交易日历（lag_days=1 → T 日发布只能用于 T+1），
  满足 PRD §6.1「T 日发布的数据只能用于 T+1」红线（§13 V-spec T2 单测）。
- ``FactorContext``：按 ``available_at`` 过滤取因子值；依赖表断更/未发布则返回 None
  （触发 §9 T7 降级，因子回落 0）。
- ``zscore``：横截面 winsorize + 标准化（§4 跨品种 B 组因子）。
- ``compute_bias_multipliers``：把 B 组因子 z 值合成为 V3.4 的两个偏置乘子
  ``position_cap_scalar`` / ``entry_gate``。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import get_engine


# ---------------------------------------------------------------------------
# 横截面标准化
# ---------------------------------------------------------------------------
def zscore(series: pd.Series, lo: float = 0.05, hi: float = 0.95) -> pd.Series:
    """winsorize([lo,hi]) + 减均值除标准差；空序列返回全 0。"""
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() < 2:
        return pd.Series(np.zeros(len(s)), index=s.index)
    ql, qh = s.quantile(lo), s.quantile(hi)
    if pd.isna(ql) or pd.isna(qh) or ql == qh:
        ql, qh = s.min(), s.max()
    w = s.clip(lower=ql, upper=qh)
    mu, sd = w.mean(), w.std(ddof=0)
    if sd is None or sd == 0 or pd.isna(sd):
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (w - mu) / sd


# ---------------------------------------------------------------------------
# asof 对齐
# ---------------------------------------------------------------------------
def asof_join(
    factor_df: pd.DataFrame,
    calendar_df: pd.DataFrame,
    lag_days: int = 1,
    by: str = "symbol",
    value_cols: list[str] | None = None,
) -> pd.DataFrame:
    """把因子值按发布时点对齐到交易日历。

    factor_df 需含 ``trade_date``(发布日) / ``symbol`` / 取值列；
    calendar_df 需含 ``trade_date``(交易日)。

    逻辑：因子有效时点 = 发布日 + lag_days（T+1 才可被策略使用），
    对每个交易日取「已生效的最新一条」因子值（merge_asof backward）。
    因此 lag_days=1 时，第 3 行（发布日=3）只能对齐到第 4 个交易日，
    第 3 个交易日取不到它自己（§13 V-spec T2 断言点）。
    """
    if factor_df is None or len(factor_df) == 0:
        return calendar_df.copy()
    f = factor_df.copy()
    f["trade_date"] = pd.to_datetime(f["trade_date"])
    cal = calendar_df.copy()
    cal["trade_date"] = pd.to_datetime(cal["trade_date"])

    f["_avail"] = f["trade_date"] + pd.Timedelta(days=lag_days)
    cal = cal.sort_values("trade_date").reset_index(drop=True)
    f = f.sort_values("_avail").reset_index(drop=True)

    cols = value_cols or [c for c in f.columns if c not in ("trade_date", "symbol", "_avail")]
    right = f[["_avail", by] + cols].rename(columns={by: "_by"})
    merged = pd.merge_asof(
        cal.rename(columns={"trade_date": "_dt", by: "_by"}) if by in cal.columns
        else cal.assign(_by=None).rename(columns={"trade_date": "_dt"}),
        right,
        left_on="_dt",
        right_on="_avail",
        by="_by" if by in cal.columns else None,
        direction="backward",
    )
    merged = merged.rename(columns={"_dt": "trade_date"})
    if "_by" in merged.columns:
        merged = merged.rename(columns={"_by": by})
    merged = merged.drop(columns=[c for c in ("_avail",) if c in merged.columns])
    return merged


# ---------------------------------------------------------------------------
# 因子上下文
# ---------------------------------------------------------------------------
@dataclass
class FactorContext:
    """按 available_at 过滤取因子值；缺失/断更 → None（触发降级）。"""

    registry: dict[str, dict] = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)

    @classmethod
    def load(cls, enabled_only: bool = True) -> "FactorContext":
        eng = get_engine()
        reg: dict[str, dict] = {}
        with eng.connect() as c:
            rows = c.execute(text(
                "SELECT factor_id, name, category, data_sources, default_weight, "
                "max_weight, horizon, lag_days, enabled FROM factor_registry"
            )).fetchall()
        for r in rows:
            if enabled_only and not r[8]:
                continue
            reg[r[0]] = {
                "factor_id": r[0], "name": r[1], "category": r[2],
                "data_sources": r[3] or [], "default_weight": float(r[4] or 0),
                "max_weight": float(r[5] or 0.3), "horizon": r[6] or "B",
                "lag_days": int(r[7] or 0),
            }
        return cls(registry=reg)

    def lookup(self, factor_id: str, symbol: str, trade_date: _dt.date) -> float | None:
        """返回该 symbol 在 trade_date 可用的最新 z 值；不可用时返回 None。

        硬性红线：``available_at > trade_date`` 的数据一律不可用（§6.1/§13 V-spec T3）。
        """
        key = (factor_id, symbol)
        if key not in self._cache:
            eng = get_engine()
            with eng.connect() as c:
                rows = c.execute(text(
                    "SELECT available_at, z_value FROM factor_value "
                    "WHERE factor_id=:f AND symbol=:s ORDER BY available_at DESC"
                ), {"f": factor_id, "s": symbol}).fetchall()
            self._cache[key] = [(r[0], r[1]) for r in rows]
        for avail, z in self._cache[key]:
            if isinstance(avail, str):
                avail = _dt.date.fromisoformat(avail[:10])
            if avail <= trade_date:
                return float(z) if z is not None else None
        return None

    def lookup_many(self, factor_ids: list[str], symbol: str, trade_date: _dt.date) -> dict[str, float | None]:
        return {fid: self.lookup(fid, symbol, trade_date) for fid in factor_ids}


# ---------------------------------------------------------------------------
# 偏置乘子合成
# ---------------------------------------------------------------------------
@dataclass
class BiasConfig:
    """因子偏置乘子配置（接入 PRD §5）。可在 config.yaml 的 factor_bias 覆盖。"""

    gate_threshold: float = 0.0          # entry_gate 低于此值 → 禁止入场
    cap_floor: float = 0.5              # position_cap_scalar 下限（最保守仓位上限）
    gate_floor: float = -1.0            # entry_gate 下限
    degrade_missing: str = "floor"      # 因子缺失时乘子取值：floor=最保守 / neutral=按0算
    max_weight_registry_sum: float = 1.0  # 校验：全因子 max_weight 之和上限（T6）


def compute_bias_multipliers(
    z_by_factor: dict[str, float | None],
    registry: dict[str, dict],
    bias: BiasConfig | None = None,
) -> dict[str, float]:
    """把 B 组因子 z 值合成两个偏置乘子。

    - ``position_cap_scalar`` = clip(1 + Σ wᵢ·zᵢ·dirᵢ, cap_floor, 1.0)：压制最大仓位上限。
    - ``entry_gate``          = clip(Σ wᵢ·zᵢ·dirᵢ, gate_floor, 1.0)：门控入场。

    因子方向 dirᵢ 由因子语义决定（如库存高→偏空→dir=-1）。本实现用 default_weight 的符号
    近似方向（>0 看多、<0 看空）；缺失因子按 degrade 策略处理（§9 T7 降级到 0/地板）。
    """
    bias = bias or BiasConfig()
    total = 0.0
    missing = False
    for fid, z in z_by_factor.items():
        meta = registry.get(fid)
        if meta is None:
            continue
        w = float(meta.get("default_weight") or 0.0)
        if z is None:
            missing = True
            if bias.degrade_missing == "floor":
                # 任一关键因子缺失 → 直接落到最保守地板
                return {"position_cap_scalar": bias.cap_floor, "entry_gate": bias.gate_floor}
            continue  # neutral：缺失按 0 计入
        total += w * z  # w 符号隐式编码方向

    cap = float(np.clip(1.0 + total, bias.cap_floor, 1.0))
    gate = float(np.clip(total, bias.gate_floor, 1.0))
    if missing and bias.degrade_missing == "neutral" and gate < bias.gate_threshold:
        gate = bias.gate_floor
    return {"position_cap_scalar": cap, "entry_gate": gate}


def validate_max_weight(registry: dict[str, dict], cap: float = 1.0) -> list[str]:
    """T6：全因子 max_weight 之和超过 cap → 返回违规因子 id 列表（非空即失败）。"""
    s = sum(float(m.get("max_weight") or 0.0) for m in registry.values())
    if s > cap + 1e-9:
        return [fid for fid, m in registry.items() if float(m.get("max_weight") or 0.0) > 0]
    return []
