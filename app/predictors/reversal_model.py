"""P1 条件反转模型 + 信号门控（PRD §18.2，v1.3 M5.5）

实证依据（§18，验证报告 2026-09-09）：
- 反转规则 OOS（TEST 2024+）dir_acc=0.535，p<1e-7，选择偏差排除
- 门控：|ret_t| > gate_pct%（默认 2.0，v1.3.3 对齐 research τ*=2.0%）才发信号；否则"无观点"
  （signaled=False，不参与集成投票、回测不计入命中分母）
- confidence 随 |ret_t| 单调上升：prob = 0.5 + 0.25×min(1, |ret_t|/2%)
- Δoi > 0 软上调 oi_boost_prob（非硬门控；oi 缺失时跳过）

上下文注入（与 LSTM 同模式）：
- m._doi：当日持仓量环比（Δoi，可 None）
"""
from __future__ import annotations

import numpy as np

from app.predictors.base import BaseModel, ModelOutput


class ReversalModel(BaseModel):
    """条件反转：大波动次日反着做，小波动不出手"""

    name = "reversal"

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        from app.core.config import get_settings

        cfg = get_settings().yaml.predict.reversal
        r = np.asarray(rets, dtype=float)
        r = r[~np.isnan(r)]
        if r.size < 20:
            raise ValueError(f"{self.name}: 数据不足 {r.size}")

        ret_t = float(r[-1])
        if abs(ret_t) <= cfg.gate_pct:
            # §18.2 门控：小波动"无观点"
            return ModelOutput(
                name=self.name,
                direction="up" if ret_t >= 0 else "down",  # 占位，融合层忽略
                prob=0.5,
                ret_point=0.0,
                ret_low=0.0,
                ret_high=0.0,
                signaled=False,
                gate_reason=f"|ret_t|={abs(ret_t):.2f}%<={cfg.gate_pct}%",
            )

        # 反转方向与幅度点估计
        direction = "down" if ret_t > 0 else "up"
        point = -ret_t * cfg.strength

        # 区间：σ 用近 20 日滚动波动（缩放防止过窄）
        sigma = float(np.std(r[-20:], ddof=1)) or 0.5
        sigma = max(sigma, 0.3)

        # confidence 随 |ret_t| 单调上升 + Δoi>0 软上调
        doi = getattr(self, "_doi", None)
        prob = 0.5 + 0.25 * min(1.0, abs(ret_t) / 2.0)
        if doi is not None and doi > 0:
            prob += cfg.oi_boost_prob
        prob = min(max(prob, 0.5), cfg.max_prob)

        return ModelOutput(
            name=self.name,
            direction=direction,
            prob=float(prob),
            ret_point=float(point),
            ret_low=float(point - 1.645 * sigma),
            ret_high=float(point + 1.645 * sigma),
            signaled=True,
            gate_reason="",
        )
