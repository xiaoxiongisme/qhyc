# -*- coding: utf-8 -*-
"""因子层（因子接入 PRD 2026-09-26）。

暴露 asof_join / FactorContext / zscore / compute_bias_multipliers 供融合策略接入。
"""
from app.factor.asof import (
    BiasConfig,
    FactorContext,
    asof_join,
    compute_bias_multipliers,
    validate_max_weight,
    zscore,
)

__all__ = [
    "BiasConfig",
    "FactorContext",
    "asof_join",
    "compute_bias_multipliers",
    "validate_max_weight",
    "zscore",
]
