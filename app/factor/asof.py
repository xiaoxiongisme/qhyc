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
import time as _time
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
    # ★ 统一两侧 merge key 的时基单位（2026-10-08 修复）
    #   症状：pandas>=2 报 `MergeError: incompatible merge keys [0]
    #   dtype('<M8[s]') and dtype('<M8[us]')`。
    #   根因：两侧单位**各自推断、互不相干**——
    #     · `pd.to_datetime` 对 `datetime.date` 对象推断为 `datetime64[s]`；
    #     · 但 `datetime64[s] + pd.Timedelta(days=n)` 会被 Timedelta 的单位
    #       （新版 pandas 为 `us`）**升精度**成 `datetime64[us]`；
    #     · 于是 left(`cal._dt`) 是 [s]、right(`f._avail`) 是 [us]，
    #       而 `merge_asof` 强制要求两侧 dtype 完全一致 → 直接抛错。
    #   为什么以前没炸：旧 pandas 统一用 `datetime64[ns]`，两侧恰好同型；
    #   pandas 2 起支持非纳秒分辨率后，这个「恰好」就没了。
    #   修法：**不写死单位**（写死 ns 会在 pandas 3 被弃用），而是让派生列
    #   跟随日历列的单位——日频数据本就只用到日精度，截断子秒无副作用。
    _key_dtype = cal["trade_date"].dtype
    if f["_avail"].dtype != _key_dtype:
        f["_avail"] = f["_avail"].astype(_key_dtype)
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

    #: ``_cache`` 的存活时长（秒）。2026-10-09 新增（PR review I3）。
    #:
    #: 原实现 ``lookup`` 首次查询后把该 (factor, symbol) 的**全量历史**永久缓存，
    #: 而 :class:`FactorContext` 又被 :mod:`app.strategies.fusion_signal` 以模块级
    #: 单例缓存（``_factor_ctx_cache``）持有 —— 两者叠加导致进程内**永不更新**：
    #: ``factor_v1v6`` 每天 16:20 写入的新因子值当天永不可见，``factor_registry``
    #: 的 enabled/weight 改动也不生效（"显示可调、实际不生效"）。
    #:
    #: TTL 与 :data:`app.strategies.fusion_signal._FACTOR_CTX_TTL_SEC` 同为 300s：
    #: 因子是低频数据（每日一批），5 分钟足够新鲜。过期后按 (factor, symbol)
    #: 重新查库——键空间是「因子数 × 品种数」，量级很小，重查成本可忽略。
    _cache_ttl_sec: float = 300.0
    _cache_ts: dict = field(default_factory=dict)

    def _cache_expired(self, key: tuple) -> bool:
        """该键的缓存是否已过期。

        ⚠ **无时间戳 = 视为有效**（不过期）。这是刻意的：调用方（含单元测试）可能
        直接预置 ``ctx._cache[...]`` 注入已知序列，此时没有 ``_cache_ts`` 记录。
        若把"无时间戳"判为过期，会强制重查库并**覆盖手工注入的数据**——
        2026-10-09 加TTL 时就踩到了这个（``test_lookup_available_at_red_line``）。
        正常查询路径每次都会写 ``_cache_ts``，故不受此影响。
        """
        ts = self._cache_ts.get(key)
        return ts is not None and (_time.time() - ts) >= self._cache_ttl_sec

    @classmethod
    def from_rows(cls, rows, enabled_only: bool = True) -> "FactorContext":
        """纯函数式构造：把注册表行 → registry dict（enabled_only 时跳过未启用项）。

        拆出来是为了可测：**`enabled=false` 必须与「该因子从未注册」逐位等价**
        （PRD T18 双向等价回归）。有 DB 才能测的东西最后一定没人测。
        """
        reg: dict[str, dict] = {}
        for r in rows:
            if enabled_only and not r[8]:
                continue
            reg[r[0]] = {
                "factor_id": r[0], "name": r[1], "category": r[2],
                "data_sources": r[3] or [], "default_weight": float(r[4] or 0),
                "max_weight": float(r[5] or 0.3), "horizon": r[6] or "B",
                "lag_days": int(r[7] or 0), "enabled": bool(r[8]),
            }
        return cls(registry=reg)

    @classmethod
    def load(cls, enabled_only: bool = True) -> "FactorContext":
        eng = get_engine()
        with eng.connect() as c:
            rows = c.execute(text(
                "SELECT factor_id, name, category, data_sources, default_weight, "
                "max_weight, horizon, lag_days, enabled FROM factor_registry"
            )).fetchall()
        return cls.from_rows(rows, enabled_only=enabled_only)

    def lookup(self, factor_id: str, symbol: str, trade_date: _dt.date) -> float | None:
        """返回该 symbol 在 trade_date 可用的最新 z 值；不可用时返回 None。

        硬性红线：``available_at > trade_date`` 的数据一律不可用（§6.1/§13 V-spec T3）。

        ★ 2026-10-09：缓存加 TTL（见 :attr:`_cache_ttl_sec`）。原实现首次查询后
          永久缓存全量历史，而调用方（``fusion_signal._factor_ctx_cache``）是模块级
          单例 ⇒ 进程内**永不更新**：当天 16:20 新写入的因子值当天永不可见，
          且因子缺失时会静默降级为地板（gate=-1.0 ⇒ 该品种永不开仓，只留一行
          warning），故障表现易被误判为"策略自然失效"。
        """
        key = (factor_id, symbol)
        if key not in self._cache or self._cache_expired(key):
            eng = get_engine()
            with eng.connect() as c:
                rows = c.execute(text(
                    "SELECT available_at, z_value FROM factor_value "
                    "WHERE factor_id=:f AND symbol=:s ORDER BY available_at DESC"
                ), {"f": factor_id, "s": symbol}).fetchall()
            self._cache[key] = [(r[0], r[1]) for r in rows]
            self._cache_ts[key] = _time.time()
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
    """T6：全因子 max_weight 之和超过 cap → 返回违规因子 id 列表（非空即失败）。

    只统计**启用**因子：未启用（enabled=false）的因子不占额度，
    否则「退役一个因子但保留 max_weight 以便回滚」会被误判为超配。
    """
    live = {fid: m for fid, m in registry.items() if m.get("enabled") is not False}
    s = sum(float(m.get("max_weight") or 0.0) for m in live.values())
    if s > cap + 1e-9:
        return [fid for fid, m in live.items() if float(m.get("max_weight") or 0.0) > 0]
    return []
