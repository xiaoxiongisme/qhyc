# -*- coding: utf-8 -*-
"""修正 hazard 估计量：会话末尾截断的 run 应为「删失」而非「结束」。
hazard(k) = 在 k 处真实结束的 run 数 / 风险集（观测长度>=k 的 run 数，含删失）。
同时输出 run-length 分布与马尔可夫几何基准对比、分状态 hazard。
"""
import numpy as np, json
from ke_common import load, save_json, NAMES

PARTS = ['2020', '2021_2025', '2026']
KMAX = 12
res = json.load(open('results_persist.json'))

for sym in ['MA', 'EG']:
    d = load(sym, PARTS)
    st, seg = d.state.values, d.seg.values
    runs = []          # (state, length, ended_by_transition)
    for s in np.unique(seg):
        seq = st[seg == s]
        cur = seq[0]; L = 1
        for x in seq[1:]:
            if x == cur: L += 1
            else: runs.append((cur, L, True)); cur = x; L = 1
        runs.append((cur, L, False))          # 会话末尾：删失
    surv = np.zeros(KMAX + 1); end = np.zeros(KMAX + 1)
    surv_s = np.zeros((8, KMAX + 1)); end_s = np.zeros((8, KMAX + 1))
    dist = np.zeros(KMAX + 1)
    for s0, L, ended in runs:
        dist[min(L, KMAX)] += 1
        for k in range(1, min(L, KMAX) + 1):
            surv[k] += 1; surv_s[s0, k] += 1
        if ended and L <= KMAX:
            end[L] += 1; end_s[s0, L] += 1
    haz = end[1:KMAX + 1] / np.maximum(surv[1:KMAX + 1], 1)
    p_stay = np.array(res[sym]['p_stay'])
    haz_mk = float((1 - p_stay).mean())
    # 马尔可夫几何基准的 run-length 分布：P(L=k) = p_stay^k (1-p_stay)，按 p_stay 分布平均
    C = np.zeros((8, 8))
    # 一阶转移矩阵重算（全样本）
    for s in np.unique(seg):
        sq = st[seg == s]
        np.add.at(C, (sq[:-1], sq[1:]), 1)
    Ptr = C / np.maximum(C.sum(1, keepdims=True), 1)
    ps = np.diag(Ptr)
    # 几何基准：P(L=k) = (1-p)·p^(k-1)，按各状态 p_stay 混合
    mk_dist = np.array([np.mean((1 - ps) * ps ** (k - 1)) for k in range(1, KMAX + 1)])
    obs_dist = dist[1:KMAX + 1] / max(dist[1:].sum(), 1)
    out = {'hazard_censor': haz.tolist(), 'surv': surv.astype(int).tolist(),
           'hazard_markov': haz_mk,
           'obs_dist': obs_dist.tolist(), 'markov_dist': mk_dist.tolist(),
           'n_runs': len(runs),
           'hazard_by_state': (end_s[1:KMAX + 1] / np.maximum(surv_s[1:KMAX + 1], 1)).tolist(),
           'mean_run': [float(np.mean([L for s0, L, e in runs if s0 == k])) for k in range(8)],
           'long_run_share': float(np.mean([L >= 5 for s0, L, e in runs])),
           'long_run_share_markov': float(np.mean(ps ** 5))}
    res[sym]['hazard_fix'] = out
    print(f'[{sym}] runs={len(runs)} hazard(删失修正) k=1..8:', [round(x, 3) for x in haz[:8]])
    print(f'  马尔可夫几何 hazard={haz_mk:.3f} | P(L>=5) 实际 {out["long_run_share"]:.4f} vs 模拟 {out["long_run_share_markov"]:.4f}')
    print(f'  obs_dist k=1..8:', [round(float(x), 4) for x in obs_dist[:8]], ' markov:', [round(float(x), 4) for x in mk_dist[:8]])

save_json(res, 'results_persist.json')
print('updated results_persist.json')
