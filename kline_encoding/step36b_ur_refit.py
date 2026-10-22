# -*- coding: utf-8 -*-
"""Step 36: 板块 15 品种 K=3 HMM（log 振幅发射，按会话等长分组批处理 Baum-Welch）
缩放 forward-backward（beta 用 alpha 的 c_t 归一）。检验「HMM 打败 8 态链」是否板块普适。
"""
import sys; sys.path.insert(0, '.')
import os
import json
import time
import numpy as np
import pandas as pd
from scipy.special import logsumexp
from scipy.stats import norm
from ke_common import load

PARTS = ['2020', '2021_2025', '2026']
SYMS = ['UR']
K = 3
N_RESTART, N_ITER, VAR_FLOOR = 10, 60, 0.02
LOG_EPS = 1e-300


def load_sk(sym):
    parts = [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
    return load(sym, parts)


def seg_groups(d):
    segs = d.groupby('seg').indices
    by_len = {}
    for s, idxs in segs.items():
        by_len.setdefault(len(idxs), []).append(np.asarray(idxs))
    return sorted(by_len.items())


def em_fit(groups, logx_fn, rng):
    xs = []
    for L, gl in groups:
        for g in gl:
            xs.append(logx_fn(g))
    X = np.concatenate(xs)
    qs = np.quantile(X, [0.2, 0.5, 0.8])
    mu = qs + rng.normal(0, 0.03, K)
    sd = np.full(K, max(float(X.std()), 0.2))
    pi = np.ones(K) / K
    A = np.full((K, K), 1.0 / K)
    logA = np.log(A); logpi = np.log(pi)
    prev_ll = -np.inf
    ll_total = -np.inf
    for it in range(N_ITER):
        acc_pi = np.zeros(K); acc_A = np.zeros((K, K)); acc_g = np.zeros(K)
        acc_gx = np.zeros(K); acc_gxx = np.zeros(K)
        ll_total = 0.0
        for L, gl in groups:
            Xb = np.stack([logx_fn(g) for g in gl])
            B = Xb.shape[0]
            E = -0.5 * ((Xb[:, :, None] - mu[None, None, :]) ** 2) / (sd[None, None, :] ** 2) \
                - np.log(sd)[None, None, :] - 0.5 * np.log(2 * np.pi)
            alpha = np.empty((B, L, K)); beta = np.empty((B, L, K)); logc = np.empty((B, L))
            a = logpi[None, :] + E[:, 0, :]
            c0 = logsumexp(a, axis=1)
            alpha[:, 0, :] = np.exp(a - c0[:, None]); logc[:, 0] = c0
            ll_total += float(c0.sum())
            for t in range(1, L):
                tmp = np.log(np.maximum(alpha[:, t - 1, :], LOG_EPS))[:, :, None] + logA[None, :, :]
                a = logsumexp(tmp, axis=1) + E[:, t, :]
                c = logsumexp(a, axis=1)
                alpha[:, t, :] = np.exp(a - c[:, None]); logc[:, t] = c
                ll_total += float(c.sum())
            beta[:, L - 1, :] = 1.0
            for t in range(L - 2, -1, -1):
                tmp = logA[None, :, :] + (E[:, t + 1, :] + np.log(np.maximum(beta[:, t + 1, :], LOG_EPS)))[:, None, :]
                beta[:, t, :] = np.exp(logsumexp(tmp, axis=2) - logc[:, t + 1][:, None])
            gamma = alpha * beta
            gamma /= np.maximum(gamma.sum(2, keepdims=True), LOG_EPS)
            for t in range(L - 1):
                tmp = (np.log(np.maximum(alpha[:, t, :], LOG_EPS))[:, :, None] + logA[None, :, :]
                       + (E[:, t + 1, :] + np.log(np.maximum(beta[:, t + 1, :], LOG_EPS)))[:, None, :])
                xi = np.exp(tmp - logsumexp(tmp.reshape(B, -1), axis=1)[:, None, None])
                acc_A += xi.sum(0)
            acc_pi += gamma[:, 0, :].sum(0)
            acc_g += gamma.sum((0, 1))
            acc_gx += (gamma * Xb[:, :, None]).sum((0, 1))
            acc_gxx += (gamma * (Xb[:, :, None] ** 2)).sum((0, 1))
        if not np.isfinite(ll_total):
            return None
        pi = np.maximum(acc_pi / max(acc_pi.sum(), 1e-9), 1e-6); pi /= pi.sum()
        A = np.maximum(acc_A / np.maximum(acc_A.sum(1, keepdims=True), 1e-9), 1e-6)
        A /= A.sum(1, keepdims=True)
        mu = acc_gx / np.maximum(acc_g, 1e-9)
        var = np.maximum(acc_gxx / np.maximum(acc_g, 1e-9) - mu ** 2, VAR_FLOOR)
        sd = np.sqrt(var)
        logA = np.log(A); logpi = np.log(pi)
        if it % 5 == 0 or it == N_ITER - 1:
            print(f'    iter {it}: logL/bar {ll_total / len(X):.4f}', flush=True)
        prev_ll = ll_total
    return {'pi': pi, 'A': A, 'mu': mu, 'sd': sd, 'logL': ll_total}


def hmm_filter(Xb, p):
    mu, sd, A, pi = p['mu'], p['sd'], p['A'], p['pi']
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


out = {}
chain = json.load(open('results_sector_chain.json', encoding='utf-8'))
for sym in SYMS:
    t0 = time.time()
    d = load_sk(sym)
    amp = d.amp.values
    floor = float(amp[amp > 0].min() / 2.0)
    logx = lambda g: np.log(np.maximum(amp[g], floor))
    groups = seg_groups(d)
    yr = d.year.values
    rng = np.random.default_rng(11)
    best = None
    for r in range(N_RESTART):
        p = em_fit(groups, logx, rng)
        if p and (best is None or p['logL'] > best['logL']):
            best = p
    p = best
    qs = np.quantile(amp[yr <= 2025], [0.2, 0.4, 0.6, 0.8])
    edges = np.concatenate([[0.0], qs, [np.inf]])
    logedges = np.log(np.maximum(edges, floor))
    Pc = np.zeros((K, 5))
    for k in range(K):
        for j in range(5):
            Pc[k, j] = norm.cdf((logedges[j + 1] - p['mu'][k]) / p['sd'][k]) \
                       - norm.cdf((logedges[j] - p['mu'][k]) / p['sd'][k])
    Pc = np.maximum(Pc, 1e-12)
    segs = d.groupby('seg').indices
    nlls = []; hits = []; occ = np.zeros(K); nbar = 0
    adj = d.adj.values
    cls = np.clip(np.digitize(amp, qs), 0, 4)
    for L, gl in groups:
        ho = [g for g in gl if yr[g[0]] == 2026 and len(g) > 1]
        if not ho:
            continue
        Xb = np.stack([logx(g) for g in ho])
        gamma = hmm_filter(Xb, p)
        for gi, g in enumerate(ho):
            pred = gamma[gi, :-1, :] @ p['A'] @ Pc
            tgt = cls[g[1:]]
            m = adj[g[1:]]
            if m.sum() < 5:
                continue
            nlls.append(-np.log2(np.maximum(pred[m], 1e-12))[np.arange(int(m.sum())), tgt[m]])
            hits.append((pred[m].argmax(1) == tgt[m]).astype(float))
            occ += gamma[gi, :-1][m].sum(0)
            nbar += int(m.sum())
    nll = float(np.concatenate(nlls).mean()) if nlls else None
    top1 = float(np.concatenate(hits).mean()) if hits else None
    occ = (occ / max(nbar, 1)).round(4).tolist()
    out[sym] = {'hmm_nll': nll, 'hmm_top1': top1, 'occ': occ,
                'mu_amp': (np.exp(p['mu']) * 100).round(4).tolist(),
                'sd_log': [round(float(x), 4) for x in p['sd']],
                'pi': [round(float(x), 4) for x in p['pi']],
                'A': [[round(float(x), 4) for x in row] for row in p['A']],
                'logL_per_bar': round(p['logL'] / len(d), 4),
                'chain_m1': chain[sym]['m1'], 'chain_m2': chain[sym]['m2'],
                'chain_marg': chain[sym]['marg']}
    print(f"{sym}: HMM {nll:.3f} vs 一阶链 {chain[sym]['m1']:.3f} 二阶 {chain[sym]['m2']:.3f} 边际 {chain[sym]['marg']:.3f} "
          f"| top1 {top1:.3f} occ {occ} mu_amp {out[sym]['mu_amp']} [{time.time()-t0:.0f}s]", flush=True)

json.dump(out, open('results_ur_refit.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved results_sector_hmm.json')
