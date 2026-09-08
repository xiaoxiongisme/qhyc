"""ARIMA 模型（PRD §5.2 统计时序类）

- statsmodels ARIMA(p,d,q) = (1,0,1)：日频收益率稳健小阶
- 点估计 + 预测标准误 → 正态 P5/P95
- ⑱ 轻模型随预测在线更新（每次预测重新 fit，窗口 250）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class ARIMAModel(BaseModel):
    name = "arima"

    def __init__(self, order: tuple[int, int, int] = (1, 0, 1), window: int = 250):
        self.order = order
        self.window = window

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        from statsmodels.tsa.arima.model import ARIMA

        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 60:
            raise ValueError(f"arima: 数据不足 {x.size}")

        try:
            model = ARIMA(x, order=self.order, trend="n")
            fitted = model.fit()
            fc = fitted.get_forecast(steps=1)
            point = float(np.asarray(fc.predicted_mean).ravel()[0])
            se = float(np.asarray(fc.se_mean).ravel()[0])
        except Exception as e:
            raise ValueError(f"arima: fit 失败 {e}") from e

        if not np.isfinite(point):
            raise ValueError("arima: 预测值非有限")
        return self._from_point_dist(self.name, point, se if se > 0 else 1.0)
