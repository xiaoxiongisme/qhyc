"""马尔可夫链模型（PRD §5.2 状态/概率类）

- 状态 = 涨/跌（⑫ 二分类起步；震荡标签后期加）
- 由历史转移矩阵给出 P(次日方向 | 今日状态)
- 幅度 = 条件历史分布的均值 + 经验分位数（P5/P95）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class MarkovModel(BaseModel):
    name = "markov"

    def __init__(self, window: int = 250, deadzone_pct: float = 0.0):
        self.window = window
        self.deadzone = deadzone_pct  # |ret|<deadzone 视为震荡（M2 默认 0=纯二分类）

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 30:
            raise ValueError(f"markov: 数据不足 {x.size}")

        states = np.where(x > self.deadzone, 1, 0)  # 1=up, 0=down
        cur = states[-1]
        nxt = states[1:]
        cur_states = states[:-1]

        # 转移计数 + Laplace 平滑
        n_up_given_cur = int(((cur_states == cur) & (nxt == 1)).sum()) + 1
        n_given_cur = int((cur_states == cur).sum()) + 2
        p_up = n_up_given_cur / n_given_cur

        direction = "up" if p_up >= 0.5 else "down"
        prob = p_up if direction == "up" else 1 - p_up

        # 条件幅度分布（次日收益 | 今日状态）
        cond = x[1:][cur_states == cur]
        if cond.size < 5:
            cond = x  # 样本太少退化为无条件分布
        point = float(cond.mean())
        lo, hi = np.quantile(cond, [0.05, 0.95])
        return ModelOutput(
            name=self.name,
            direction=direction,
            prob=float(min(max(prob, 0.5), 0.999)),
            ret_point=point,
            ret_low=float(lo),
            ret_high=float(hi),
        )
