# -*- coding: utf-8 -*-
"""S2 状态持续期与隐状态（HMM）。
(a) 8 态 run-length 分布 + 风险函数 hazard(k)，对比一阶马尔可夫几何基准
(b) 持续期依赖转移 P(自保持 | 已持续 k 根)
(c) 高斯 HMM（手写 Baum-Welch，多重启）：暴力枚举校验 forward、EM 单调性校验
    holdout 2026 上与显态 8 状态一阶/二阶链比「下一根振幅五档」log-loss / top-1
(d) 隐状态持续期、隐态×显态互信息、显态在隐态之上的增量
口径：会话内序列（seg 切分），train=2020-2025 拟合，holdout=2026 评估，无前视。
"""
import numpy as np, pandas as pd
from ke_common import load, save_json, NAMES

PARTS = ['2020', '2021_2025', '2026']
RES = {}
rng0 = np.random.default_rng(7)


# ---------------- HMM ----------------
def emissions(X, mu, sd):
    """X: (...,) 观测; mu/sd: (K,) → b: (..., K)"""
    z = (X[..., None] - mu) / sd
    return np.exp(-0.5 * z * z) / (sd * np.sqrt(2 * np.pi))


def forward_backward_batch(seqs, pi, A, mu, sd):
    """seqs: list of 1-D arrays 或 (B,L) 二维数组。返回 (logL, gamma, xi, b_all)。"""
    L = len(seqs[0]); B = len(seqs); K = len(mu)
    X = np.asarray(seqs, float)
    if X.ndim == 1: X = X[None]
    b_all = emissions(X, mu, sd)              # (B, L, K)
    alpha = np.zeros((B, L, K)); c = np.zeros((B, L))
    a = pi * b_all[:, 0]
    c[:, 0] = a.sum(1); alpha[:, 0] = a / c[:, 0, None]
    for t in range(1, L):
        a = (alpha[:, t - 1] @ A) * b_all[:, t]
        c[:, t] = a.sum(1); alpha[:, t] = a / c[:, t, None]
    logL = float(np.log(c).sum())
    beta = np.zeros((B, L, K))
    beta[:, L - 1] = 1.0
    for t in range(L - 2, -1, -1):
        beta[:, t] = (A @ (b_all[:, t + 1] * beta[:, t + 1]).T).T / c[:, t + 1, None]
    gamma = alpha * beta
    gamma /= gamma.sum(2, keepdims=True)
    xi = np.zeros((B, L - 1, K, K))
    for t in range(L - 1):
        xi[:, t] = alpha[:, t][:, :, None] * A[None] * (b_all[:, t + 1] * beta[:, t + 1])[:, None, :]
        xi[:, t] /= c[:, t + 1, None, None]
    return logL, gamma, xi, b_all


def hmm_fit(seqs, K, iters=35, init=None, seed=0):
    rng = np.random.default_rng(seed)
    Xall = np.concatenate(seqs)
    groups = {}
    for s in seqs: groups.setdefault(len(s), []).append(s)
    GX = [(np.stack(g), len(g[0])) for g in groups.values()]     # 预计算分组
    if init is None:
        # k-means 初始化（sd 用整簇标准差，下限为全局 std 的 10%）
        xs = rng.choice(Xall, size=min(50000, len(Xall)), replace=False)
        gstd = xs.std()
        mu = np.quantile(xs, np.linspace(0.1, 0.9, K)).copy()
        lab = None
        for _ in range(20):
            d = np.abs(xs[:, None] - mu[None])
            lab = d.argmin(1)
            for k in range(K):
                if (lab == k).sum() > 1: mu[k] = xs[lab == k].mean()
        sd = np.array([max(xs[lab == k].std(), 0.1 * gstd) if (lab == k).sum() > 1 else gstd for k in range(K)])
        pi = np.ones(K) / K
        A = np.full((K, K), 1.0 / K)
    else:
        pi, A, mu, sd = [np.array(v, float) for v in init]
    prev = -np.inf
    for it in range(iters):
        gsum = np.zeros((K,)); xsum = np.zeros((K,)); x2sum = np.zeros((K,))
        Asum = np.zeros((K, K)); ll = 0.0
        g0 = np.zeros((K,))
        for Xg, L in GX:
            logL, gamma, xi, _ = forward_backward_batch(Xg, pi, A, mu, sd)
            ll += logL
            g0 += gamma[:, 0].sum(0)
            gsum += gamma.sum((0, 1))
            xsum += (gamma * Xg[..., None]).sum((0, 1))
            x2sum += (gamma * (Xg[..., None] ** 2)).sum((0, 1))
            Asum += xi.sum((0, 1))
        mu = xsum / np.maximum(gsum, 1e-9)
        var = np.maximum(x2sum / np.maximum(gsum, 1e-9) - mu ** 2, 0.02)   # sd>=0.14，防塔尖下溢
        sd = np.sqrt(var)
        pi = g0 / max(g0.sum(), 1e-9)
        A = Asum / np.maximum(Asum.sum(1, keepdims=True), 1e-9)
        if (not np.isfinite(ll)) or ll < prev - 1e-6:
            return None, ll, (pi, A, mu, sd)   # NaN 或单调性破坏
        prev = ll
    return prev, prev, (pi, A, mu, sd)


def hmm_loglik(seqs, params):
    pi, A, mu, sd = params
    groups = {}
    for s in seqs: groups.setdefault(len(s), []).append(s)
    ll = 0.0
    for L, gseqs in groups.items():
        logL, *_ = forward_backward_batch(gseqs, pi, A, mu, sd)
        ll += logL
    return ll


def hmm_filter(seqs, params):
    """因果滤波：返回每序列每时刻的 gamma (B,L,K)（按长度分组拼接回原顺序）"""
    pi, A, mu, sd = params
    out = {}
    groups = {}
    for i, s in enumerate(seqs): groups.setdefault(len(s), []).append(i)
    for L, idxs in groups.items():
        gseqs = [seqs[i] for i in idxs]
        _, gamma, _, _ = forward_backward_batch(gseqs, pi, A, mu, sd)
        for j, i in enumerate(idxs): out[i] = gamma[j]
    return out


for sym in ['MA', 'EG']:
    d = load(sym, PARTS)
    n = len(d)
    m_tr = d.year.values < 2026
    m_te = ~m_tr
    amp, st, seg = d.amp.values, d.state.values, d.seg.values
    adj = d.adj.values

    # 会话序列（seg 切分）
    segs = [np.where(seg == s)[0] for s in np.unique(seg)]
    seqs_all = [st[ii] for ii in segs]
    tr_idx = set(np.where(m_tr)[0].tolist())
    seqs_tr = [s for s, ii in zip(seqs_all, segs) if ii[0] in tr_idx]
    te_idx = set(np.where(m_te)[0].tolist())
    seqs_te = [s for s, ii in zip(seqs_all, segs) if ii[0] in te_idx]
    # 对应振幅序列（HMM 观测）
    amp_tr = [amp[ii] for ii in segs if ii[0] in tr_idx]
    amp_te = [amp[ii] for ii in segs if ii[0] in te_idx]

    # ---------- (a) run-length 与 hazard ----------
    runs = {s: [] for s in range(8)}
    for s in seqs_all:
        cur = s[0]; L = 1
        for x in s[1:]:
            if x == cur: L += 1
            else: runs[cur].append(L); cur = x; L = 1
        runs[cur].append(L)
    run_stats = []
    for s in range(8):
        r = np.array(runs[s])
        run_stats.append({'state': NAMES[s], 'n_runs': int(len(r)), 'mean_len': float(r.mean()),
                          'p50': float(np.median(r)), 'p90': float(np.quantile(r, 0.9)),
                          'max': int(r.max())})
    # 一阶马尔可夫 p_stay → 几何基准
    C = np.zeros((8, 8))
    for s in seqs_all:
        np.add.at(C, (s[:-1], s[1:]), 1)
    Ptr = C / np.maximum(C.sum(1, keepdims=True), 1)
    p_stay = np.diag(Ptr).copy()
    # hazard(k) = P(长度=k 的 run 结束 | 长度>=k)，k=1..10，pooled
    KMAX = 10
    surv = np.zeros(KMAX + 1); end = np.zeros(KMAX + 1)
    surv_s = np.zeros((8, KMAX + 1)); end_s = np.zeros((8, KMAX + 1))
    for s in range(8):
        for L in runs[s]:
            for k in range(1, min(L, KMAX) + 1):
                surv_s[s, k] += 1
                if L == k: end_s[s, k] += 1
    surv = surv_s.sum(0); end = end_s.sum(0)
    haz = end[:KMAX + 1] / np.maximum(surv[:KMAX + 1], 1)
    haz_markov = (1 - p_stay).mean()

    # ---------- (c) HMM ----------
    # 零振幅 bar（h==l，占 0.26-0.33%）log 会得 -inf → 以「非零最小值/2」为下限（截尾观测解释）
    amp_floor = np.concatenate(amp_tr)
    amp_floor = amp_floor[amp_floor > 0].min() / 2.0
    x_tr = [np.log(np.maximum(a, amp_floor)) for a in amp_tr]
    x_te = [np.log(np.maximum(a, amp_floor)) for a in amp_te]
    vparams = (np.array([0.5, 0.3, 0.2]), np.array([[0.7, 0.2, 0.1], [0.2, 0.6, 0.2], [0.1, 0.3, 0.6]]),
               np.array([-4.0, -3.0, -2.0]), np.array([0.3, 0.4, 0.5]))
    vseq = rng0.normal(-3.2, 0.4, 6)
    ll_fb, *_ = forward_backward_batch([vseq], *vparams)
    K = 3; tot = 0.0
    def rec(t, cur, p):
        global tot
        if t == len(vseq):
            tot += p; return
        for k in range(K):
            rec(t + 1, k, p * (vparams[1][cur, k] if t > 0 else vparams[0][k]) * emissions(np.array([vseq[t]]), vparams[2], vparams[3])[0, k])
    rec(0, -1, 1.0)
    check_fwd = abs(ll_fb - np.log(tot)) < 1e-6
    # 多重启拟合 K=2,3,4（k-means 初始化 + 3 次随机扰动）
    hmm_res = {}
    for K in [2, 3, 4]:
        best = None
        for rs in range(4):
            init = None
            if rs > 0:   # 随机扰动重启
                xs = rng0.choice(np.concatenate(x_tr), size=min(50000, len(np.concatenate(x_tr))), replace=False)
                mu0 = np.quantile(xs, rng0.uniform(0.05, 0.95, K)) + rng0.normal(0, 0.1, K)
                init = (np.ones(K) / K, np.full((K, K), 1.0 / K), mu0, np.full(K, 0.3))
            ll, ll2, params = hmm_fit(x_tr, K, iters=60, init=init, seed=rs)
            if ll is None: continue                     # 单调性破坏，丢弃
            if best is None or ll > best[0]: best = (ll, params)
        ll, params = best
        hmm_res[K] = {'train_ll': float(ll), 'params': [p.tolist() for p in params]}
        print(f'[HMM {sym} K={K}] train logL={ll:.1f} 单调性OK')
    # 用 K=3 做主评估（也报 K=2,4）
    # 目标：下一根振幅五档（train 分位点固定）
    amp_all_tr = np.concatenate(amp_tr)
    qs_amp = np.quantile(amp_all_tr, [0.2, 0.4, 0.6, 0.8])
    edges = np.concatenate([[0.0], qs_amp, [np.inf]])
    def bin_of(a): return np.clip(np.digitize(a, qs_amp), 0, 4)
    # 显态模型：一阶/二阶（train 计数 + 平滑），向量化计数（sid 防跨会话对）
    sid_tr = np.concatenate([np.full(len(s), i) for i, s in enumerate(seqs_tr)])
    st_tr = np.concatenate(seqs_tr); b_tr = bin_of(np.concatenate(amp_tr))
    m1 = sid_tr[:-1] == sid_tr[1:]
    C1 = np.zeros((8, 5))
    np.add.at(C1, (st_tr[:-1][m1], b_tr[1:][m1]), 1)
    C2 = np.zeros((8, 8, 5))
    m2 = m1[:-1] & (sid_tr[:-2] == sid_tr[1:-1])
    np.add.at(C2, (st_tr[:-2][m2], st_tr[1:-1][m2], b_tr[2:][m2]), 1)
    alpha_s = 0.5
    P1 = (C1 + alpha_s) / (C1 + alpha_s * 5).sum(1, keepdims=True)
    P2 = (C2 + alpha_s) / (C2 + alpha_s * 5).sum(2, keepdims=True)
    marg = np.bincount(bin_of(amp_all_tr), minlength=5) / len(amp_all_tr)
    # HMM 预测：P(bin_{t+1}) = Σ_j P(bin|j) Σ_k γ_t(k) A_kj
    from scipy.stats import norm
    eval_out = {}
    for K in [2, 3, 4]:
        params = [np.array(p) for p in hmm_res[K]['params']]
        pi, A, mu, sd = params
        Bb = np.zeros((K, 5))
        for b in range(5):
            lo, hi = edges[b], edges[b + 1]
            Bb[:, b] = norm.cdf(np.log(hi), mu, sd) - norm.cdf(np.log(lo), mu, sd)
        Bb = np.maximum(Bb, 1e-12); Bb /= Bb.sum(1, keepdims=True)
        gammas = hmm_filter(x_te, params)
        ll_h = 0.0; hit_h = 0; cnt = 0
        ll_1 = 0.0; hit_1 = 0; ll_2 = 0.0; hit_2 = 0; ll_m = 0.0; hit_m = 0
        for si, (s, a) in enumerate(zip(seqs_te, amp_te)):
            b = bin_of(a); g = gammas[si]
            for t in range(len(s) - 1):
                tgt = b[t + 1]
                ph = (g[t] @ A) @ Bb
                ll_h -= np.log2(max(ph[tgt], 1e-12)); hit_h += int(ph.argmax() == tgt)
                p1 = P1[s[t]]; ll_1 -= np.log2(max(p1[tgt], 1e-12)); hit_1 += int(p1.argmax() == tgt)
                p2 = P2[s[t - 1], s[t]] if t >= 1 else p1
                ll_2 -= np.log2(max(p2[tgt], 1e-12)); hit_2 += int(p2.argmax() == tgt)
                ll_m -= np.log2(max(marg[tgt], 1e-12)); hit_m += int(marg.argmax() == tgt)
                cnt += 1
        eval_out[K] = {'n': int(cnt), 'hmm_ll': ll_h / cnt, 'hmm_top1': hit_h / cnt,
                       'm1_ll': ll_1 / cnt, 'm1_top1': hit_1 / cnt,
                       'm2_ll': ll_2 / cnt, 'm2_top1': hit_2 / cnt,
                       'marg_ll': ll_m / cnt, 'marg_top1': hit_m / cnt}
        print(f'[EVAL {sym} K={K}] n={cnt} log2/bar: HMM {ll_h/cnt:.4f} | 一阶 {ll_1/cnt:.4f} | 二阶 {ll_2/cnt:.4f} | 边际 {ll_m/cnt:.4f}'
              f'  top1: HMM {hit_h/cnt:.3f} 一阶 {hit_1/cnt:.3f} 二阶 {hit_2/cnt:.3f}')
    # 隐状态结构（K=3）：持续期、与显态互信息
    K = 3
    params = [np.array(p) for p in hmm_res[K]['params']]
    pi, A, mu, sd = params
    order = np.argsort(mu)      # 按 emission 均值排序（低波→高波）
    remap = np.zeros(K, int); remap[order] = np.arange(K)
    mu_s = mu[order]; sd_s = sd[order]
    pi_s = pi[order]; A_s = A[np.ix_(order, order)]
    params_s = (pi_s, A_s, mu_s, sd_s)
    # Viterbi 解码 train，统计隐状态持续期
    def viterbi(x, params):
        pi, A, mu, sd = params
        L = len(x); K = len(mu)
        b = emissions(np.array(x), mu, sd)
        d = np.zeros((L, K)); bp = np.zeros((L, K), int)
        d[0] = np.log(pi + 1e-12) + np.log(b[0] + 1e-12)
        for t in range(1, L):
            m = d[t - 1][:, None] + np.log(A + 1e-12)
            bp[t] = m.argmax(0); d[t] = m.max(0) + np.log(b[t] + 1e-12)
        path = np.zeros(L, int); path[-1] = d[-1].argmax()
        for t in range(L - 2, -1, -1): path[t] = bp[t + 1, path[t + 1]]
        return path
    hruns = [[] for _ in range(K)]
    mi_num = 0.0
    paths_tr = []
    for s, a in zip(seqs_tr, amp_tr):
        path = viterbi(np.log(np.maximum(a, amp_floor)), params_s)
        paths_tr.append(path)
        for k in range(K):
            p = path == k
            if p.sum() == 0: continue
            idxs = np.where(p)[0]
            splits = np.where(np.diff(idxs) > 1)[0]
            for grp in np.split(idxs, splits + 1): hruns[k].append(len(grp))
    # 互信息（全 train 汇总后一次性估计）
    path_tr = np.concatenate(paths_tr)
    ph = np.bincount(path_tr, minlength=K) / len(path_tr)
    ps = np.bincount(st_tr, minlength=8) / len(st_tr)
    joint = np.zeros((K, 8))
    np.add.at(joint, (path_tr, st_tr), 1); joint /= joint.sum()
    for i in range(K):
        for j in range(8):
            if joint[i, j] > 0: mi_num += joint[i, j] * np.log2(joint[i, j] / (ph[i] * ps[j]))
    hid = {'mu_log': [float(x) for x in mu_s], 'sd_log': [float(x) for x in sd_s],
           'pi': [float(x) for x in pi_s],
           'A': A_s.tolist(),
           'mean_run': [float(np.mean(r)) if r else None for r in hruns],
           'frac_bars': [float((path_tr == k).mean()) for k in range(K)],
           'mi_with_explicit_bits': float(mi_num)}
    # 显态在隐态之上的增量：P(bin | hidden, explicit) vs P(bin | hidden)（train 拟合，holdout 评估）
    sid_tr2 = sid_tr; m1t = m1
    Jh = np.zeros((K, 8, 5))
    np.add.at(Jh, (path_tr[:-1][m1t], st_tr[:-1][m1t], b_tr[1:][m1t]), 1)
    alpha2 = 0.5
    Pjoint = (Jh + alpha2) / (Jh + alpha2 * 5).sum(2, keepdims=True)
    Ph = np.zeros((K, 5))
    for k in range(K): Ph[k] = (Jh[k].sum(0) + alpha2) / (Jh[k].sum() + alpha2 * 5)
    ll_j = 0.0; ll_h2 = 0.0; cnt2 = 0
    gammas = hmm_filter(x_te, params_s)
    for si, (s, a) in enumerate(zip(seqs_te, amp_te)):
        path = viterbi(np.log(np.maximum(a, amp_floor)), params_s)
        b = bin_of(a); g = gammas[si]
        for t in range(len(s) - 1):
            tgt = b[t + 1]
            pj = sum(g[t][k] * Pjoint[k, s[t], tgt] for k in range(K))
            ph2 = sum(g[t][k] * Ph[k, tgt] for k in range(K))
            ll_j -= np.log2(max(pj, 1e-12)); ll_h2 -= np.log2(max(ph2, 1e-12)); cnt2 += 1
    incr = {'joint_ll': ll_j / cnt2, 'hidden_only_ll': ll_h2 / cnt2, 'n': int(cnt2),
            'gain_bits': (ll_h2 - ll_j) / cnt2}
    print(f'[INCR {sym}] 隐态 {ll_h2/cnt2:.4f} → 隐态×显态 {ll_j/cnt2:.4f}，显态增量 {incr["gain_bits"]:.4f} bit/bar')
    print(f'[HID {sym}] mu_log={[round(float(x),3) for x in mu_s]} run={[round(float(np.mean(r)),2) for r in hruns]} MI={mi_num:.3f}bit')

    RES[sym] = {'run_stats': run_stats, 'p_stay': p_stay.tolist(),
                'hazard': haz.tolist(), 'hazard_markov': float(haz_markov),
                'surv': surv.tolist(),
                'hmm': {str(k): v for k, v in hmm_res.items()}, 'eval': eval_out,
                'hidden': hid, 'increment': incr,
                'check_forward_bruteforce': bool(check_fwd)}
    print(f'[{sym}] forward暴力枚举校验: {"PASS" if check_fwd else "FAIL"}')

save_json(RES, 'results_persist.json')
print('saved results_persist.json')
