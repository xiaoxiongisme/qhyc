"""模型基类与输出契约（M2）

ModelOutput：
- direction: up / down（⑫ 涨跌二分类）
- prob: 该方向的概率 [0.5, 1.0]
- ret_point: 点估计幅度 %（⑩ 结算价涨跌幅口径）
- ret_low / ret_high: 分位区间（P5 / P95）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class ModelOutput:
    name: str
    direction: str            # up / down
    prob: float               # direction 概率
    ret_point: float          # 点估计 %
    ret_low: float            # P5 %
    ret_high: float           # P95 %
    # §18.2（v1.3）M5.5 信号门控：False=该模型本点"无观点"，
    # 不参与集成投票、回测不计入 dir_acc 分母（记 gate_reason）
    signaled: bool = True
    gate_reason: str = ""
    # §18.7（v1.3.2）M6c 波动率产品化：σ_t 条件波动率预测（%）；
    # 仅 GARCH 等波动率专长模型填充，None=该模型不输出。
    # vol_low/vol_high 为 |收益| 的 P5/P95（半正态分位），与 ret_low/high（带符号）语义不同
    vol_forecast: float | None = None
    vol_low: float | None = None
    vol_high: float | None = None


class BaseModel(ABC):
    """预测模型基类：fit 在线更新（⑱ 轻模型随预测在线更新）

    extra：§16.3 传导特征逐日 DataFrame（可选，ML 类模型使用）；
    其余模型忽略。全部特征仅含 as_of 之前数据（§16.7 ① 防前视）。
    """

    name: str = "base"

    @abstractmethod
    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        """输入收益率序列（%）+ 可选传导特征，输出下一交易日预测"""

    @staticmethod
    def _from_point_dist(name: str, ret_point: float, sigma: float) -> ModelOutput:
        """由正态点估计 + σ 构造标准输出（方向 / 概率 / P5 / P95）"""
        sigma = max(float(sigma), 1e-6)
        direction = "up" if ret_point >= 0 else "down"
        from scipy.stats import norm

        # P(方向成立) = P(同号 | N(ret, σ))
        z = abs(ret_point) / sigma
        prob = 0.5 + 0.5 * (2 * norm.cdf(z) - 1)
        prob = min(max(prob, 0.5), 0.999)
        lo = ret_point - 1.645 * sigma
        hi = ret_point + 1.645 * sigma
        return ModelOutput(
            name=name,
            direction=direction,
            prob=float(prob),
            ret_point=float(ret_point),
            ret_low=float(lo),
            ret_high=float(hi),
        )

    @staticmethod
    def _from_quantiles(name: str, point: float, lo: float, hi: float) -> ModelOutput:
        """由经验分位数构造标准输出（P5/P95 与方向一致）"""
        if lo > hi:
            lo, hi = hi, lo
        direction = "up" if point >= 0 else "down"
        if direction == "up":
            prob = 0.9 if point > 0 else 0.5
        else:
            prob = 0.9 if point < 0 else 0.5
        return ModelOutput(
            name=name,
            direction=direction,
            prob=prob,
            ret_point=float(point),
            ret_low=float(lo),
            ret_high=float(hi),
        )

    @staticmethod
    def _from_empirical_resid(
        name: str, point: float, resid: np.ndarray, vol_scale: float = 1.0
    ) -> ModelOutput:
        """审计 P1-3 区间校准：用训练残差的**经验分位**构造 P5/P95

        替代正态假设 σ（正态假设下实际越界率远超 10%，覆盖失效）。
        resid 为模型在留出集上的残差（同分布假设下，预测 + 残差分位 ≈ 预测区间）。
        vol_scale：波动率缩放（复验修正）——留出期波动 ≠ 预测期波动时按
        current_vol/resid_vol 比例放大区间，修正波动率聚集导致的覆盖失效。
        """
        r = np.asarray(resid, dtype=float)
        r = r[~np.isnan(r)]
        scale = min(max(float(vol_scale), 0.5), 3.0)
        if r.size < 30:
            # 样本不足退回正态
            return BaseModel._from_point_dist(name, point, float(np.std(r) if r.size > 1 else 1.0) * scale)
        lo = point + float(np.quantile(r, 0.05)) * scale
        hi = point + float(np.quantile(r, 0.95)) * scale
        if lo > hi:
            lo, hi = hi, lo
        direction = "up" if point >= 0 else "down"
        from scipy.stats import norm

        sigma = float(np.std(r, ddof=1)) or 1.0
        z = abs(point) / sigma
        prob = 0.5 + 0.5 * (2 * norm.cdf(z) - 1)
        return ModelOutput(
            name=name,
            direction=direction,
            prob=float(min(max(prob, 0.5), 0.999)),
            ret_point=float(point),
            ret_low=float(lo),
            ret_high=float(hi),
        )
