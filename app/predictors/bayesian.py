"""贝叶斯更新模型（PRD §5.2 状态/概率类）

Beta-Binomial 方向后验：
- 先验：历史全部涨跌计数 → Beta(α, β)
- 证据：近 k 日上涨天数（动量证据）
- 后验：Beta(α+k, β+k̄-k) → P(up) 后验均值
- 幅度：近 20 日条件均值 + 经验分位
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class BayesianModel(BaseModel):
    name = "bayesian"

    def __init__(self, window: int = 250, evidence_days: int = 5, point_window: int = 20):
        self.window = window
        self.evidence_days = evidence_days
        self.point_window = point_window

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 40:
            raise ValueError(f"bayesian: 数据不足 {x.size}")

        # 先验：全历史涨跌
        ups = int((x > 0).sum())
        downs = int((x <= 0).sum())
        alpha_prior, beta_prior = max(ups, 1), max(downs, 1)

        # 证据：近 evidence_days 日上涨数
        recent = x[-self.evidence_days :]
        k = int((recent > 0).sum())

        # 后验 Beta
        a_post = alpha_prior + k
        b_post = beta_prior + (self.evidence_days - k)
        p_up = a_post / (a_post + b_post)

        direction = "up" if p_up >= 0.5 else "down"
        prob = p_up if direction == "up" else 1 - p_up

        # 幅度：近 point_window 日均值 + 分位（保守）
        cond = x[-self.point_window :]
        point = float(cond.mean())
        lo, hi = np.quantile(cond, [0.05, 0.95])
        # 方向与幅度对齐（贝叶斯幅度天然弱信息，不做强断言）
        if direction == "up" and point < 0:
            point = abs(point) * 0.2
        if direction == "down" and point > 0:
            point = -abs(point) * 0.2
        return ModelOutput(
            name=self.name,
            direction=direction,
            prob=float(min(max(prob, 0.5), 0.999)),
            ret_point=point,
            ret_low=float(lo),
            ret_high=float(hi),
        )
