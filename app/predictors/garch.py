"""GARCH 模型（PRD §5.2 统计时序类）

arch 包 GARCH(1,1)：波动率聚类建模，输出均值预测 + 条件波动率区间
（方向贡献弱是预期行为，主要贡献幅度/风险区间 §5.2）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


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
        return self._from_point_dist(self.name, point, sigma)
