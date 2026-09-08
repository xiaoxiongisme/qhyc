"""蒙特卡洛模拟模型（PRD §5.2 状态/概率类）

历史自助抽样（bootstrap）路径模拟：
- 从近 window 日收益经验分布重抽样 10000 条一日路径
- P(up) = 模拟中 >0 占比；ret_point = 中位数；P5/P95 = 分位数
- 非参数、不假设正态，保留肥尾（§5.2：路径分布 → 涨跌概率 + 分位幅度）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class MonteCarloModel(BaseModel):
    name = "montecarlo"

    def __init__(self, window: int = 250, n_sims: int = 10000, seed: int = 42):
        self.window = window
        self.n_sims = n_sims
        self.seed = seed

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 40:
            raise ValueError(f"montecarlo: 数据不足 {x.size}")

        rng = np.random.default_rng(self.seed)
        sims = rng.choice(x, size=self.n_sims, replace=True)
        p_up = float((sims > 0).mean())
        point, lo, hi = np.quantile(sims, [0.5, 0.05, 0.95])

        direction = "up" if p_up >= 0.5 else "down"
        prob = p_up if direction == "up" else 1 - p_up
        return ModelOutput(
            name=self.name,
            direction=direction,
            prob=float(min(max(prob, 0.5), 0.999)),
            ret_point=float(point),
            ret_low=float(lo),
            ret_high=float(hi),
        )
