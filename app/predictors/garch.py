"""GARCH 模型（PRD §5.2 统计时序类 + §18.7 v1.3.2 波动率产品化）

arch 包 GARCH(1,1)：波动率聚类建模。
§18.7（M6c）：σ_t 条件波动率预测成为**一等输出**——
- vol_forecast = σ_{t+1}（次日条件波动率，%）
- |ret| 区间 [P5, P95] 按半正态分位：|X|~HalfNormal(σ) →
  P5 = σ×Φ⁻¹(0.525) ≈ 0.0627σ，P95 = σ×Φ⁻¹(0.975) = 1.96σ
- 方向贡献弱是预期行为（主要贡献幅度/风险区间，§5.2）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


# 半正态分位系数（|X| ~ HalfNormal(σ)）
_HALFNORMAL_P05 = 0.06270678   # Φ⁻¹(0.525)
_HALFNORMAL_P95 = 1.95996398   # Φ⁻¹(0.975)


class GARCHModel(BaseModel):
    name = "garch"

    def __init__(self, window: int = 250, p: int = 1, q: int = 1):
        self.window = window
        self.p, self.q = p, q

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        from arch import arch_model

        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 60:
            raise ValueError(f"garch: 数据不足 {x.size}")

        # 收益率量纲 %，rescale=False 保留；dist=normal
        am = arch_model(x, vol="GARCH", p=self.p, q=self.q, mean="Constant", rescale=False)
        res = am.fit(disp="off", show_warning=False)
        fc = res.forecast(horizon=1, reindex=False)
        point = float(np.asarray(fc.mean).ravel()[0])
        var = float(np.asarray(fc.variance).ravel()[0])
        sigma = float(np.sqrt(max(var, 1e-8)))

        out = self._from_point_dist(self.name, point, sigma)
        # §18.7：波动率三件套（σ_t + |ret| P5/P95 半正态分位）
        # 注意 ret_low/high 保持"带符号收益分位"语义（quantile_hit 统计用），
        # |ret| 区间独立放 vol_low/vol_high
        out.vol_forecast = sigma
        out.vol_low = float(sigma * _HALFNORMAL_P05)
        out.vol_high = float(sigma * _HALFNORMAL_P95)
        return out
