"""复杂系统 / 信息论特征（PRD §5.1）：Hurst 指数、样本熵

- Hurst：R/S 分析，判定趋势（H>0.5）vs 均值回复（H<0.5）→ 状态门控输入（§5.3）
- 样本熵：市场可预测性度量（低熵 → 模型更可信，M3 置信度加权用）
均为轻量纯 numpy 实现，满足日频在线更新（⑱ 轻模型随预测在线更新）。
"""
from __future__ import annotations

import numpy as np


def hurst_exponent(rets: np.ndarray, min_chunk: int = 8) -> float | None:
    """R/S 分析 Hurst 指数

    rets: 收益率序列（%）。返回 H ∈ [0,1]；数据不足返回 None。
    """
    x = np.asarray(rets, dtype=float)
    x = x[~np.isnan(x)]
    if x.size < min_chunk * 4:
        return None
    x = x - x.mean()

    chunk_sizes = []
    rs_means = []
    n_total = x.size
    size = min_chunk
    while size <= n_total // 2 and size >= min_chunk:
        n_chunks = n_total // size
        if n_chunks < 2:
            break
        rs_vals = []
        for i in range(n_chunks):
            seg = x[i * size : (i + 1) * size]
            mean = seg.mean()
            dev = seg - mean
            z = np.cumsum(dev)
            r = z.max() - z.min()
            s = seg.std(ddof=0)
            if s > 1e-12:
                rs_vals.append(r / s)
        if rs_vals:
            chunk_sizes.append(size)
            rs_means.append(float(np.mean(rs_vals)))
        size *= 2

    if len(chunk_sizes) < 3:
        return None
    logs = np.log(np.array(chunk_sizes, dtype=float))
    lrs = np.log(np.array(rs_means, dtype=float))
    # log(R/S) = H * log(n) + c
    H = float(np.polyfit(logs, lrs, 1)[0])
    return max(0.0, min(1.0, H))


def sample_entropy(rets: np.ndarray, m: int = 2, r_ratio: float = 0.2) -> float | None:
    """样本熵 SampEn(m, r)：越低 → 序列越可预测

    r = r_ratio * std(rets)。数据不足返回 None。
    """
    x = np.asarray(rets, dtype=float)
    x = x[~np.isnan(x)]
    n = x.size
    if n < m + 2:
        return None
    r = r_ratio * x.std(ddof=1)

    def _count_sim(template_len: int) -> tuple[int, int]:
        count = 0
        self_count = 0
        for i in range(n - template_len):
            tpl = x[i : i + template_len]
            for j in range(i + 1, n - template_len + 1):
                seg = x[j : j + template_len]
                if np.max(np.abs(tpl - seg)) <= r:
                    count += 1
                    if i == j:
                        self_count += 1
        return count, self_count

    # 向量长度 m 与 m+1 的匹配数（不含自匹配）
    def _matches(L: int) -> int:
        cnt = 0
        for i in range(n - L):
            tpl = x[i : i + L]
            for j in range(i + 1, n - L + 1):
                if np.max(np.abs(tpl - x[j : j + L])) <= r:
                    cnt += 1
        return cnt

    if r <= 0:
        return None
    b = _matches(m)
    a = _matches(m + 1)
    if b == 0 or a == 0:
        return None
    return float(-np.log(a / b))


def market_state(
    hurst: float | None, trend_th: float, revert_th: float
) -> str:
    """Hurst → 状态门控（§5.3）：trend / mean_revert / neutral"""
    if hurst is None:
        return "neutral"
    if hurst > trend_th:
        return "trend"
    if hurst < revert_th:
        return "mean_revert"
    return "neutral"
