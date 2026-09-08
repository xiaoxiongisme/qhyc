"""卡尔曼滤波模型（PRD §5.2 统计时序类）

局部水平模型（Local Level）：
- 状态：潜在收益水平 x_t；观测：ret_t = x_t + v, v~N(0,R)
- 状态转移：x_t = x_{t-1} + w, w~N(0,Q)
- 预测：x_{n+1|n} = x_{n|n}（随机游走水平），σ = sqrt(P_{n+1|n})
纯 numpy 实现，在线更新（⑱）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class KalmanModel(BaseModel):
    name = "kalman"

    def __init__(self, window: int = 250, q: float = 0.1, r_ratio: float = 1.0):
        self.window = window
        self.q = q                    # 过程噪声（水平漂移）
        self.r_ratio = r_ratio        # 观测噪声 = r_ratio × 观测方差

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 40:
            raise ValueError(f"kalman: 数据不足 {x.size}")

        R = float(np.var(x, ddof=1)) * self.r_ratio
        Q = self.q
        # 初始
        state = float(x[0])
        P = R
        innovations = []   # 审计 P1-3：新息序列（经验分位区间用）
        for z in x[1:]:
            # 预测
            P_pred = P + Q
            # 更新
            K = P_pred / (P_pred + R)
            innovations.append(z - state)
            state = state + K * (z - state)
            P = (1 - K) * P_pred

        point = state                      # 随机游走：下期水平 = 当前滤波水平
        # 审计 P1-3：新息经验分位区间（正态 σ 区间覆盖失效）
        return self._from_empirical_resid(self.name, point, np.asarray(innovations))
