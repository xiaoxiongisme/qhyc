# -*- coding: utf-8 -*-
"""诊断 FG/UR 的 top-1 异常：用已存 HMM 参数重算 holdout 预测分布直方图 vs 实际分布"""
import sys; sys.path.insert(0, '.')
import os
import json
import numpy as np
import pandas as pd
from scipy.special import logsumexp
from scipy.stats import norm
from ke_common import load

PARTS = ['2020', '2021_2025', '2026']
K = 3
LOG_EPS = 1e-300


def load_sk(sym):
    parts = [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
    return load(sym, parts)


def hmm_filter(Xb, p):
    mu = np.log(np.array(p['mu_amp']) / 100.0); sd = np.array(p['sd_log'])
    A, pi = np.array(p['A']), np.array(p['pi'])
    B, L = Xb.shape
    E = -0.5 * ((Xb[:, :, None] - mu[None, None, :]) ** 2) / (sd[None, None, :] ** 2) \
        - np.log(sd)[None, None, :] - 0.5 * np.log(2 * np.pi)
    logA = np.log(A)
    alpha = np.empty((B, L, K))
    a = np.log(pi)[None, :] + E[:, 0, :]
    alpha[:, 0, :] = np.exp(a - logsumexp(a, axis=1, keepdims=True))
    for t in range(1, L):
        tmp = np.log(np.maximum(alpha[:, t - 1, :], LOG_EPS))[:, :, None] + logA[None, :, :]
        a = logsumexp(tmp, axis=1) + E[:, t, :]
        alpha[:, t, :] = np.exp(a - logsumexp(a, axis=1, keepdims=True))
    return alpha / np.maximum(alpha.sum(2, keepdims=True), LOG_EPS)


for sym in ('FG', 'UR'):
    p = json.load(open('results_sector_hmm.json', encoding='utf-8'))[sym]
    d = load_sk(sym)
    amp = d.amp.values
    floor = float(amp[amp > 0].min() / 2.0)
    yr = d.year.values
    qs = np.quantile(amp[yr <= 2025], [0.2, 0.4, 0.6, 0.8])
    edges = np.concatenate([[0.0], qs, [np.inf]])
    logedges = np.log(np.maximum(edges, floor))
    mu = np.log(np.array(p['mu_amp']) / 100.0)
    sd = np.array(p['sd_log'])
    Pc = np.zeros((K, 5))
    for k in range(K):
        for j in range(5):
            Pc[k, j] = norm.cdf((logedges[j + 1] - mu[k]) / sd[k]) - norm.cdf((logedges[j] - mu[k]) / sd[k])
    Pc = np.maximum(Pc, 1e-12)
    print(f'=== {sym} mu_log={mu.round(3)} sd={sd.round(3)} (由 mu_amp 还原)')
    print('  P(class|state) 每行:', [np.round(Pc[k], 3).tolist() for k in range(K)])
    cls = np.clip(np.digitize(amp, qs), 0, 4)
    adj = d.adj.values
    segs = d.groupby('seg').indices
    by_len = {}
    for s, idxs in segs.items():
        by_len.setdefault(len(idxs), []).append(np.asarray(idxs))
    pred_hist = np.zeros(5); real_hist = np.zeros(5); argmax_hist = np.zeros(5)
    nlls = []
    for L, gl in sorted(by_len.items()):
        ho = [g for g in gl if yr[g[0]] == 2026 and len(g) > 1]
        if not ho:
            continue
        Xb = np.stack([np.log(np.maximum(amp[g], floor)) for g in ho])
        gamma = hmm_filter(Xb, p)
        for gi, g in enumerate(ho):
            pred = gamma[gi, :-1, :] @ np.array(p['A']) @ Pc
            tgt = cls[g[1:]]
            m = adj[g[1:]]
            if m.sum() < 5:
                continue
            pm = pred[m]
            pred_hist += pm.sum(0)
            real_hist += np.bincount(tgt[m], minlength=5)
            argmax_hist += np.bincount(pm.argmax(1), minlength=5)
            nlls.append(-np.log2(np.maximum(pm[np.arange(len(pm)), tgt[m]], 1e-12)))
    tot = real_hist.sum()
    print(f'  预测分布均值: {(pred_hist/tot).round(3)}')
    print(f'  实际分布:     {(real_hist/tot).round(3)}')
    print(f'  argmax 分布:  {(argmax_hist/tot).round(3)}')
    print(f'  nll {np.concatenate(nlls).mean():.3f}  top1 {(argmax_hist[np.arange(5)]/tot).round(3)}')
