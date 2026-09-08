"""傅里叶变换模型（PRD §5.2 变换类）

提取周期成分外推下一期：
- 去均值 → rFFT → 取幅值前 k 个频率（排除直流分量）
- 用 A_k·cos(2πf_k·t + φ_k) 解析外推 t = n 处值
- 残差 σ 构造 P5/P95 区间
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class FourierModel(BaseModel):
    name = "fourier"

    def __init__(self, top_k: int = 5, window: int = 250):
        self.top_k = top_k
        self.window = window

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 30:
            raise ValueError(f"fourier: 数据不足 {x.size}")

        n = x.size
        mu = x.mean()
        y = x - mu

        spec = np.fft.rfft(y)
        freqs = np.fft.rfftfreq(n)
        amp = np.abs(spec)
        # 排除直流（index 0），取 top_k 频率
        order = np.argsort(amp[1:])[::-1][: self.top_k] + 1
        kept = order[amp[order] > 1e-9]

        # 残差 σ（未保留成分的能量）
        recon = np.zeros(n)
        t = np.arange(n)
        for k in kept:
            ang = np.angle(spec[k])
            recon += amp[k] / n * np.cos(2 * np.pi * freqs[k] * t + ang) * 2
        resid = y - recon
        sigma = float(resid.std(ddof=1)) if n > 2 else 1.0

        # 外推 t = n
        point = mu
        for k in kept:
            ang = np.angle(spec[k])
            point += (
                amp[k] / n * np.cos(2 * np.pi * freqs[k] * n + ang) * 2
            )
        return self._from_point_dist(self.name, float(point), sigma)
