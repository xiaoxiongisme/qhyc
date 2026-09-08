"""小波变换模型（PRD §5.2 变换类）

多尺度去噪 + AR(1) 外推：
- db4 小波 3 层分解，细节系数软阈值去噪（突变点信息保留在细节层）
- 去噪后序列拟合 AR(1)，外推下一期
- σ = 细节系数（高频波动）std
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class WaveletModel(BaseModel):
    name = "wavelet"

    def __init__(self, wavelet: str = "db4", level: int = 3, window: int = 250):
        self.wavelet = wavelet
        self.level = level
        self.window = window

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        import pywt

        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 40:
            raise ValueError(f"wavelet: 数据不足 {x.size}")

        coeffs = pywt.wavedec(x, self.wavelet, level=self.level)
        # 细节系数软阈值去噪（universal threshold）
        detail = np.concatenate(coeffs[1:])
        sigma = float(np.median(np.abs(detail)) / 0.6745) if detail.size else 1.0
        uthresh = sigma * np.sqrt(2 * np.log(x.size))
        coeffs_denoised = [coeffs[0]] + [
            pywt.threshold(c, value=uthresh, mode="soft") for c in coeffs[1:]
        ]
        smooth = pywt.waverec(coeffs_denoised, self.wavelet)
        smooth = smooth[: x.size]

        # AR(1) 外推
        a, b = smooth[:-1], smooth[1:]
        var_a = float(np.var(a))
        if var_a < 1e-12:
            phi, intercept = 0.0, float(np.mean(b))
        else:
            phi = float(np.cov(a, b)[0, 1] / var_a)
            intercept = float(b.mean() - phi * a.mean())
        point = intercept + phi * smooth[-1]
        resid = b - (intercept + phi * a)
        # 审计 P1-3：AR(1) 残差经验分位区间
        return self._from_empirical_resid(self.name, point, resid)
