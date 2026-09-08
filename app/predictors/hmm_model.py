"""HMM 隐马尔可夫模型（PRD §5.2 状态/概率类）

hmmlearn GaussianHMM(n_components=3)：识别牛/熊/震荡隐藏状态
- 在线 fit（⑱ 轻模型）
- 预测：当前状态后验 × 各状态下一期均值/方差混合
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class HMMModel(BaseModel):
    name = "hmm"

    def __init__(self, n_states: int = 3, window: int = 250):
        self.n_states = n_states
        self.window = window

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        from hmmlearn.hmm import GaussianHMM

        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 80:
            raise ValueError(f"hmm: 数据不足 {x.size}")

        X = x.reshape(-1, 1)
        model = GaussianHMM(
            n_components=self.n_states, covariance_type="full",
            n_iter=100, random_state=42,
        )
        try:
            model.fit(X)
        except Exception as e:
            raise ValueError(f"hmm: fit 失败 {e}") from e

        # 当前时刻状态后验
        post = model.predict_proba(X)[-1]              # [n_states]
        # 各状态均值/方差（正态发射）
        means = model.means_.ravel()                   # [n_states]
        covars = model.covars_.ravel()                 # full→(n,1,1) ravel ok

        point = float(np.sum(post * means))
        var = float(np.sum(post * (covars + (means - point) ** 2)))
        return self._from_point_dist(self.name, point, float(np.sqrt(max(var, 1e-8))))
