# -*- coding: utf-8 -*-
"""多维度分析：矩阵/线性代数之外的七个新维度。
D1 信息论与压缩  D2 记忆深度(马尔可夫阶数)  D3 图论  D4 频域节律
D5 结构稳定性/变点  D6 长程标度  D7 条件结构与尾部相依
"""
import pandas as pd, numpy as np, json, heapq
from scipy.stats import f_oneway, chi2_contingency

OUT = '.'
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
SIDX = {s: i for i, s in enumerate(STATES)}
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
ZONG = {2: 1, 1: 2, 5: 6, 6: 5}      # 综卦: 影线换边
CUO = {3: 4, 4: 3, 0: 7, 7: 0, 2: 5, 5: 2, 1: 6, 6: 1}   # 错卦: 三爻全反


def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, '01', '00')
    up = np.where(h > np.maximum(o, c), '01', '00')
    lo = np.where(l < np.minimum(o, c), '01', '00')
    return np.char.add(np.char.add(body, up), lo)


def load(sym, parts):
    ds = []
    for f in parts:
        ds.append(pd.read_csv(f'{sym}_{f}_1m_raw.csv', parse_dates=['datetime']))
    d = pd.concat(ds, ignore_index=True).sort_values('datetime').reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    rng = h - l
    safe = np.where(rng > 0, rng, 1.0)
    d['state'] = np.array([SIDX[x] for x in encode_ohlc(o, h, l, c)])
    d['chg'] = (c - o) / o * 100.0
    d['amp'] = rng / o * 100.0
    d['clv'] = np.where(rng > 0, (c - l) / safe, 0.5)
    gap = d.datetime.diff().dt.total_seconds().values
    d['adj'] = np.concatenate([[False], gap[1:] == 60.0])
    d['year'] = d.datetime.dt.year.values
    d['td'] = (d.datetime + pd.to_timedelta((d.datetime.dt.hour >= 21).astype(int), unit='D')).dt.date.values
    d['dow'] = pd.to_datetime(pd.Series(d.td)).dt.dayofweek.values
    d['month'] = d.datetime.dt.month.values
    seg = np.concatenate([[0], np.cumsum(gap[1:] != 60.0)])
    d['seg'] = seg
    d['seq'] = d.groupby('seg').cumcount().values
    seg_hour = d.groupby('seg').datetime.first().dt.hour
    BASE = {9: 0, 10: 75, 13: 135, 21: 225}
    d['slot'] = d.seq + d.seg.map(seg_hour).map(BASE).values
    return d


def qbin(x, k):
    qs = np.quantile(x, [(i + 1) / k for i in range(k - 1)])
    return np.clip(np.digitize(x, qs), 0, k - 1)


def js_div(p, q):
    m = 0.5 * (p + q)
    def kl(a, b):
        nz = a > 0
        return float((a[nz] * np.log2(a[nz] / b[nz])).sum())
    return 0.5 * kl(p, m) + 0.5 * kl(q, m)


def adj_pairs(d):
    m = d.adj.values[1:]
    return d.state.values[:-1][m], d.state.values[1:][m], np.where(m)[0] + 1


def contiguous_blocks(d, minlen=8):
    adj = d.adj.values
    blocks, cur = [], [0]
    for i in range(1, len(adj)):
        if adj[i]:
            cur.append(i)
        else:
            if len(cur) >= minlen:
                blocks.append(np.array(cur))
            cur = [i]
    if len(cur) >= minlen:
        blocks.append(np.array(cur))
    return blocks


def block_entropy(seqs, k):
    """H(X_{t+1} | 前 k 个状态)，seqs 为会话内连续状态序列列表"""
    C = np.zeros((8 ** k, 8))
    for s in seqs:
        if len(s) <= k:
            continue
        ctx = np.zeros(len(s) - k, np.int64)
        for b in range(k):
            ctx = ctx * 8 + s[b:len(s) - k + b]
        np.add.at(C, (ctx, s[k:]), 1)
    tot = C.sum()
    if tot == 0:
        return None
    pc = C.sum(1) / tot
    Cc = C / np.maximum(C.sum(1, keepdims=True), 1)
    return float((pc * -(Cc * np.log2(np.maximum(Cc, 1e-12))).sum(1)).sum())


def dfa_pooled(seqs, scales):
    """按会话分别去趋势、池化 RMS（窗口不跨会话接缝）"""
    out = []
    for s in scales:
        rms = []
        for x in seqs:
            if len(x) < 4 * s:
                continue
            y = np.cumsum(x - x.mean())
            nseg = len(y) // s
            idx = (np.arange(nseg) * s).astype(int)
            for a, b in zip(idx[:-1], idx[1:]):
                seg = y[a:b]
                t = np.arange(len(seg))
                p = np.polyfit(t, seg, 1)
                rms.append(np.sqrt(np.mean((seg - np.polyval(p, t)) ** 2)))
        if rms:
            out.append((np.log2(s), np.log2(np.mean(rms))))
    return np.array(out)


def pooled_acf(seqs, lags):
    """跨会话池化的自相关（同会话内配对，不跨接缝）"""
    out = {}
    for k in lags:
        n = sx = sy = sxx = syy = sxy = 0.0
        for x in seqs:
            if len(x) <= k:
                continue
            a, b = x[:-k], x[k:]
            n += len(a); sx += a.sum(); sy += b.sum()
            sxx += (a * a).sum(); syy += (b * b).sum(); sxy += (a * b).sum()
        den = np.sqrt(max((n * sxx - sx ** 2) * (n * syy - sy ** 2), 1e-18))
        out[k] = float((n * sxy - sx * sy) / den)
    return out


def variance_ratio(d, ks):
    """VR(k)=Var(k步收益)/(k·Var(1步收益))，按会话算再取中位数"""
    chg = d.chg.values
    segs = contiguous_blocks(d, minlen=60)
    vr = {}
    for k in ks:
        vals = []
        for blk in segs:
            x = chg[blk]
            if len(x) < 4 * k:
                continue
            c = np.concatenate([[0.0], np.cumsum(x)])
            m = len(x) // k
            rk = c[k:m * k + 1:k] - c[0:m * k:k]
            v1 = x.var(ddof=1)
            if v1 > 0 and len(rk) > 4:
                vals.append(rk.var(ddof=1) / (k * v1))
        vr[k] = float(np.median(vals)) if vals else None
    return vr


RES = {}
TRAIN = ['2020', '2021_2025']
TEST = ['2026']

for sym in ['MA', 'EG']:
    dtr, dte = load(sym, TRAIN), load(sym, TEST)
    d = pd.concat([dtr, dte], ignore_index=True)
    R = {'sym': sym, 'n_train': int(len(dtr)), 'n_test': int(len(dte))}
    st, amp, chg = d.state.values, d.amp.values, d.chg.values
    adj, yr = d.adj.values, d.year.values
    pu, pn, pidx = adj_pairs(d)

    # ============ D1 信息论 / 压缩 ============
    H = np.zeros((8, 8)); np.add.at(H, (pu, pn), 1)
    P = H / np.maximum(H.sum(1, keepdims=True), 1)
    pi = H.sum(1) / H.sum()
    HX = float(-(pi[pi > 0] * np.log2(pi[pi > 0])).sum())
    rowH = -(P * np.log2(np.maximum(P, 1e-12))).sum(1)
    Hcond = float((pi * rowH).sum())
    seqs_all = [st[b] for b in contiguous_blocks(d, minlen=4)]
    blk = {k: block_entropy(seqs_all, k) for k in [1, 2, 3]}
    R['entropy'] = {
        'H_state': HX, 'H_next_given_cur': Hcond, 'block': blk,
        'redundancy': HX - blk[3],
        'bits_per_bar': blk[3], 'code_bits': 6.0,
        'compress_ratio': blk[3] / 6.0,
        'eff_alphabet': float(np.exp(HX)), 'row_entropy': rowH.tolist()}
    er_year = {}
    for y in np.unique(yr):
        m = (yr[:-1] == y) & adj[1:]
        Hy = np.zeros((8, 8)); np.add.at(Hy, (st[:-1][m], st[1:][m]), 1)
        Py = Hy / np.maximum(Hy.sum(1, keepdims=True), 1)
        piy = Hy.sum(1) / max(Hy.sum(), 1)
        er_year[int(y)] = float((piy * -(Py * np.log2(np.maximum(Py, 1e-12))).sum(1)).sum())
    R['entropy']['by_year'] = er_year

    # ============ D2 记忆深度: 马尔可夫阶数 (2026 holdout) ============
    trs = [dtr.state.values[b] for b in contiguous_blocks(dtr, minlen=8)]
    tes = [dte.state.values[b] for b in contiguous_blocks(dte, minlen=8)]

    def ctx_of(s, k):
        c = np.zeros(len(s) - k, np.int64)
        for b in range(k):
            c = c * 8 + s[b:len(s) - k + b]
        return c

    def eval_order(k, alpha=0.1):
        C = np.zeros((8 ** k, 8))
        for s in trs:
            if len(s) <= k:
                continue
            np.add.at(C, (ctx_of(s, k), s[k:]), 1)
        tot, cnt, hit, tot1 = 0.0, 0, 0, 0
        for s in tes:
            if len(s) <= k:
                continue
            c = ctx_of(s, k)
            p = (C[c] + alpha) / (C[c].sum(1, keepdims=True) + 8 * alpha)
            tot += -np.log2(p[np.arange(len(c)), s[k:]]).sum()
            cnt += len(c)
            if k >= 1:
                hit += int((C[c].argmax(1) == s[k:]).sum()); tot1 += len(c)
        out = {'loss': tot / max(cnt, 1)}
        if k >= 1:
            out['top1_acc'] = hit / max(tot1, 1)
        return out

    ev = {k: eval_order(k) for k in [0, 1, 2, 3]}
    R['memory'] = {
        'holdout': {str(k): v for k, v in ev.items()},
        'gain_1_over_0': ev[0]['loss'] - ev[1]['loss'],
        'gain_2_over_1': ev[1]['loss'] - ev[2]['loss'],
        'gain_3_over_2': ev[2]['loss'] - ev[3]['loss'],
        'order1_top1_acc': ev[1].get('top1_acc'),
        'persistence_acc': float(np.mean([np.mean(s[1:] == s[:-1]) for s in tes])),
        'n_train_bars': int(sum(len(s) for s in trs)),
        'n_test_bars': int(sum(len(s) for s in tes))}

    # ============ D3 图论 ============
    dmp = 0.15
    pr = np.ones(8) / 8
    for _ in range(300):
        pr_new = (1 - dmp) * P.T @ pr + dmp / 8
        if np.abs(pr_new - pr).max() < 1e-12:
            pr = pr_new; break
        pr = pr_new
    zsig = np.zeros((8, 8))
    for i in range(8):
        ni = H[i].sum()
        if ni < 30:
            continue
        for j in range(8):
            p0 = pi[j]
            se = np.sqrt(max(p0 * (1 - p0) / ni, 1e-18))
            zsig[i, j] = (P[i, j] - p0) / se
    reach = (H > 0) | np.eye(8, dtype=bool)
    Rk = reach.copy()
    for _ in range(8):
        Rk = Rk | (Rk @ reach)
    irreducible = bool(Rk.all())
    Wg = P + P.T
    comm = list(range(8))
    def modularity(c):
        m2 = Wg.sum(); q = 0.0
        for cc in set(c):
            idx = [i for i in range(8) if c[i] == cc]
            q += Wg[np.ix_(idx, idx)].sum() / m2 - (Wg[idx].sum() / m2) ** 2
        return q
    q0 = modularity(comm)
    improved = True
    while improved:
        improved = False
        best_q, best_move = q0, None
        for i in range(8):
            for j in range(i + 1, 8):
                if comm[i] == comm[j]:
                    continue
                c2 = comm.copy(); c2[j] = c2[i]
                q = modularity(c2)
                if q > best_q + 1e-9:
                    best_q, best_move = q, c2
        if best_move is not None:
            comm, q0, improved = best_move, best_q, True
    with np.errstate(divide='ignore'):
        Lw = -np.log(np.maximum(P, 1e-12))
    def dij(src):
        dist = [1e9] * 8; dist[src] = 0
        pq = [(0, src)]
        while pq:
            dd, u = heapq.heappop(pq)
            if dd > dist[u]:
                continue
            for v in range(8):
                nd = dd + Lw[u, v]
                if nd < dist[v]:
                    dist[v] = nd; heapq.heappush(pq, (nd, v))
        return dist
    sp = np.array([dij(s) for s in range(8)])
    R['graph'] = {
        'pagerank': pr.tolist(), 'P': P.tolist(), 'pi': pi.tolist(),
        'z_matrix': zsig.tolist(), 'n_sig_edges': int((np.abs(zsig) > 3).sum()),
        'n_sig_pos': int((zsig > 3).sum()), 'n_sig_neg': int((zsig < -3).sum()),
        'irreducible': irreducible, 'communities': comm, 'modularity': float(q0),
        'mean_path': float(sp[np.isfinite(sp)].mean()),
        'diameter': float(np.nanmax(sp[np.isfinite(sp)]))}

    # ============ D4 频域 / 节律 ============
    slot_ok = ~np.isnan(d.slot.values)
    sl = d.slot.values[slot_ok].astype(int)
    am = d.amp.values[slot_ok]
    prof345 = np.zeros(345); cnt345 = np.zeros(345)
    np.add.at(prof345, (sl,), am); np.add.at(cnt345, (sl,), 1)
    prof345 /= np.maximum(cnt345, 1)
    prof69 = prof345.reshape(69, 5).mean(1)
    F = np.abs(np.fft.rfft(prof69 - prof69.mean()))
    fr = np.fft.rfftfreq(69, d=5.0)
    order_pk = np.argsort(F[1:])[::-1][:3] + 1
    peaks = [(float(1.0 / fr[k]), float(F[k] / F[1:].sum())) for k in order_pk]
    dowv = d.dow.values[slot_ok]
    dow_ok = dowv < 5
    blk69 = sl // 5
    DOW = np.zeros((5, 69)); cntD = np.zeros((5, 69))
    np.add.at(DOW, (dowv[dow_ok], blk69[dow_ok]), am[dow_ok])
    np.add.at(cntD, (dowv[dow_ok], blk69[dow_ok]), 1)
    DOW /= np.maximum(cntD, 1)
    Uw, Sw, Vtw = np.linalg.svd(DOW - DOW.mean(), full_matrices=False)
    evr_dow = (Sw ** 2 / (Sw ** 2).sum()).tolist()
    mon = np.zeros(12); cm = np.zeros(12)
    np.add.at(mon, (d.month.values - 1,), d.amp.values); np.add.at(cm, (d.month.values - 1,), 1)
    mon /= np.maximum(cm, 1)
    tdc = pd.factorize(d.td)[0]
    nd = int(tdc.max()) + 1
    dayamp = np.zeros(nd); dc = np.zeros(nd)
    np.add.at(dayamp, (tdc,), d.amp.values); np.add.at(dc, (tdc,), 1)
    dayamp /= np.maximum(dc, 1)
    Fd = np.abs(np.fft.rfft(dayamp - dayamp.mean()))
    frd = np.fft.rfftfreq(nd, d=1.0)
    pk_d = np.argsort(Fd[1:])[::-1][:3] + 1
    peaks_day = [(float(1.0 / frd[k]), float(Fd[k] / Fd[1:].sum())) for k in pk_d]
    Fm, pm = f_oneway(*[d.amp.values[d.month.values == m] for m in range(1, 13)])
    R['freq'] = {
        'profile69': prof69.tolist(), 'peaks_min': peaks,
        'dow_matrix': DOW.tolist(), 'dow_evr': evr_dow, 'dow_mean': DOW.mean(1).tolist(),
        'month_mean': mon.tolist(), 'month_F': float(Fm), 'month_p': float(pm),
        'n_days': nd, 'day_peaks': peaks_day,
        'open_slot_share': float(prof345[:60].mean() / prof345.mean()),
        'night_share': float(prof345[225:].mean() / prof345.mean())}

    # ============ D5 结构稳定性 / 变点 ============
    Pys = {}
    for y in np.unique(yr):
        m = adj[1:] & (yr[:-1] == y)
        Cy = np.zeros((8, 8)); np.add.at(Cy, (st[:-1][m], st[1:][m]), 1)
        Pys[int(y)] = Cy / np.maximum(Cy.sum(1, keepdims=True), 1)
    fro = {int(y): float(np.linalg.norm(Pys[y] - P) / np.linalg.norm(P)) for y in Pys}
    m_tr = (yr[:-1] < 2026) & adj[1:]
    Htr = np.zeros((8, 8)); np.add.at(Htr, (st[:-1][m_tr & adj[1:]], st[1:][m_tr & adj[1:]]), 1)
    Ptr = Htr / np.maximum(Htr.sum(1, keepdims=True), 1)
    fro_26 = float(np.linalg.norm(Pys[2026] - Ptr) / np.linalg.norm(Ptr))
    cells = []
    for y in np.unique(yr):
        m = adj[1:] & (yr[:-1] == y)
        Cy = np.zeros(64); np.add.at(Cy, (st[:-1][m] * 8 + st[1:][m],), 1)
        cells.append(Cy)
    chi2, pval, dof, _ = chi2_contingency(np.array(cells))
    i_edge = SIDX['000100']
    p_edge = Ptr[i_edge, i_edge]
    win, step = 2000, 500
    cus = []
    for i in range(0, len(pu) - win, step):
        w = pu[i:i + win] == i_edge
        n1 = int(w.sum())
        if n1 < 30:
            continue
        p_hat = float((pn[i:i + win][w] == i_edge).mean())
        z = (p_hat - p_edge) / np.sqrt(max(p_edge * (1 - p_edge) / n1, 1e-18))
        cus.append((int(i), float(z)))
    m_te = (yr[:-1] == 2026) & adj[1:]
    ll_tr = float(-np.log2(np.maximum(Ptr[st[:-1][m_tr & adj[1:]], st[1:][m_tr & adj[1:]]], 1e-12)).mean())
    ll_te = float(-np.log2(np.maximum(Ptr[st[:-1][m_te], st[1:][m_te]], 1e-12)).mean())
    R['stability'] = {
        'Fro_by_year': fro, 'Fro_2026_vs_train': fro_26,
        'chi2': float(chi2), 'chi2_p': float(pval), 'chi2_dof': int(dof),
        'cusum_edge': cus, 'edge': '000100->000100', 'edge_p_train': float(p_edge),
        'logloss_train': ll_tr, 'logloss_2026': ll_te, 'surprise_gap': ll_te - ll_tr}

    # ============ D6 长程标度 ============
    sess = [amp[b] for b in contiguous_blocks(d, minlen=60)]
    dfa1 = dfa_pooled(sess, [4, 6, 8, 12, 16, 20, 24])
    alpha1 = float(np.polyfit(dfa1[:, 0], dfa1[:, 1], 1)[0]) if len(dfa1) > 2 else None
    dfad = dfa_pooled([dayamp], [4, 8, 16, 32, 64, 128, 256])
    alphad = float(np.polyfit(dfad[:, 0], dfad[:, 1], 1)[0]) if len(dfad) > 2 else None
    vr = variance_ratio(d, [2, 5, 10, 30])
    amp_acf = pooled_acf(sess, [1, 2, 5, 10, 20, 60])
    day_acf = pooled_acf([dayamp], [1, 5, 20])
    hi = (amp > np.quantile(amp, 0.9)).astype(float)
    ac_hi = [float(np.corrcoef(hi[:-k], hi[k:])[0, 1]) for k in [1, 5, 30, 120]]
    R['scaling'] = {
        'dfa_1m_alpha': alpha1, 'dfa_1m_points': dfa1.tolist(),
        'dfa_day_alpha': alphad, 'dfa_day_points': dfad.tolist(),
        'var_ratio': vr, 'amp_acf': amp_acf, 'day_acf': day_acf,
        'hi_state_acf': ac_hi,
        'n_sessions': len(sess), 'cat_len': int(sum(len(s) for s in sess))}

    # ============ D7 条件结构 ============
    amp5 = qbin(amp, 5)
    body3 = np.where(chg > 0, 2, np.where(chg == 0, 1, 0))
    cond_amp = np.zeros((8, 5)); np.add.at(cond_amp, (pu, amp5[pidx]), 1)
    cond_amp /= np.maximum(cond_amp.sum(1, keepdims=True), 1)
    cond_dir = np.zeros((8, 3)); np.add.at(cond_dir, (pu, body3[pidx]), 1)
    cond_dir /= np.maximum(cond_dir.sum(1, keepdims=True), 1)
    cur_dir = pu // 4
    rev_by = {}
    for s in range(8):
        m = pu == s
        if m.sum() > 0:
            rev_by[int(s)] = float(((body3[pidx][m] == 2) != (cur_dir[m] == 1)).mean())
    js_zong = [js_div(cond_amp[s], cond_amp[t]) for s, t in ZONG.items()]
    js_cuo = [js_div(cond_amp[s], cond_amp[t]) for s, t in CUO.items()]
    R['cond'] = {
        'cond_amp': cond_amp.tolist(), 'cond_dir': cond_dir.tolist(),
        'row_max_amp': cond_amp.max(1).tolist(),
        'reversal_by_state': rev_by,
        'js_zong_mean': float(np.mean(js_zong)), 'js_cuo_mean': float(np.mean(js_cuo))}
    RES[sym] = R
    print(f'===== {sym} 完成 train={R["n_train"]:,} test={R["n_test"]:,} =====')

# ---------- 跨品种尾部相依 ----------
dMA = pd.merge(load('MA', TRAIN + TEST)[['datetime', 'amp']],
               load('EG', TRAIN + TEST)[['datetime', 'amp']], on='datetime', suffixes=('_MA', '_EG'))
aMA, aEG = dMA.amp_MA.values, dMA.amp_EG.values
tail = {}
for q in [0.90, 0.95, 0.99]:
    tMA = aMA > np.quantile(aMA, q)
    tEG = aEG > np.quantile(aEG, q)
    both = float((tMA & tEG).mean()); exp = float(tMA.mean() * tEG.mean())
    tail[f'{int(q*100)}'] = {
        'joint': both, 'indep': exp, 'ratio': both / max(exp, 1e-12),
        'cond_MA_given_EG': float(tMA[tEG].mean()), 'cond_EG_given_MA': float(tEG[tMA].mean())}
lead_tail = {}
tMA = (aMA > np.quantile(aMA, 0.95)).astype(int)
tEG = (aEG > np.quantile(aEG, 0.95)).astype(int)
n = len(tMA)
for k in range(-5, 6):
    if k >= 0:
        lead_tail[k] = float((tMA[:n - k] & tEG[k:]).mean()) if k < n else None
    else:
        lead_tail[k] = float((tMA[-k:] & tEG[:n + k]).mean())
RES['tail'] = {'quantiles': tail, 'lead': lead_tail, 'n_common': int(n)}

json.dump(RES, open(f'{OUT}/results_dimensions.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ---------- 摘要 ----------
for sym in ['MA', 'EG']:
    r = RES[sym]
    print(f'\n===== {sym} 多维度摘要 =====')
    e = r['entropy']
    print('[D1 信息论] H(状态)=%.3f bit  H(下一态|当前态)=%.3f bit  块熵(k=1/2/3)=%s' % (
        e['H_state'], e['H_next_given_cur'], {k: round(v, 3) for k, v in e['block'].items()}))
    print('  每根实际信息=%.3f bit / 6 bit → 压缩率=%.1f%%  有效字母表=%.2f 个态' % (
        e['bits_per_bar'], e['compress_ratio'] * 100, e['eff_alphabet']))
    print('  分年熵率:', {k: round(v, 3) for k, v in e['by_year'].items()})
    m = r['memory']
    print('[D2 记忆深度] holdout log2 loss bits/根:', {k: round(v['loss'], 4) for k, v in m['holdout'].items()})
    print('  增益: 1阶-0阶=%.4f  2阶-1阶=%.4f  3阶-2阶=%.4f' % (
        m['gain_1_over_0'], m['gain_2_over_1'], m['gain_3_over_2']))
    print('  order-1 top1准确率=%.3f  持续性基线=%.3f' % (m['order1_top1_acc'], m['persistence_acc']))
    g = r['graph']
    print('[D3 图论] 显著边(3σ)=%d (正%d/负%d)  不可约=%s  社区=%s  模块度=%.3f  平均路径=%.2f bit' % (
        g['n_sig_edges'], g['n_sig_pos'], g['n_sig_neg'], g['irreducible'],
        g['communities'], g['modularity'], g['mean_path']))
    print('  PageRank top3:', [(STATES[i] + TRIG[STATES[i]], round(v, 3)) for i, v in
          sorted(enumerate(g['pagerank']), key=lambda x: -x[1])[:3]])
    f = r['freq']
    print('[D4 频域] 日内谱峰(周期分钟):', [(round(p, 1), round(s, 3)) for p, s in f['peaks_min']])
    print('  星期x时段 SVD evr top3:', [round(x, 3) for x in f['dow_evr'][:3]],
          ' 周一~周五均值:', [round(x, 4) for x in f['dow_mean']])
    print('  月均振幅(%):', [round(x, 4) for x in f['month_mean']], ' F=%.1f p=%.2g' % (f['month_F'], f['month_p']))
    print('  日振幅谱峰(周期交易日):', [(round(p, 1), round(s, 3)) for p, s in f['day_peaks']],
          ' 开盘段占比=%.3f 夜盘占比=%.3f' % (f['open_slot_share'], f['night_share']))
    s = r['stability']
    print('[D5 稳定性] 跨年Fro:', {k: round(v, 3) for k, v in s['Fro_by_year'].items()},
          ' 2026 vs 训练=%.3f' % s['Fro_2026_vs_train'])
    print('  chi2齐性=%.0f (p=%.2g, dof=%d)  2026 log-loss=%.4f vs 训练=%.4f (惊讶差=%+.4f)' % (
        s['chi2'], s['chi2_p'], s['chi2_dof'], s['logloss_2026'], s['logloss_train'], s['surprise_gap']))
    sc = r['scaling']
    a1 = sc['dfa_1m_alpha']; ad = sc['dfa_day_alpha']
    print('[D6 标度] DFA α: 1分钟=%s  日=%s  方差比=%s' % (
        f'{a1:.3f}' if a1 else 'NA', f'{ad:.3f}' if ad else 'NA',
        {k: round(v, 3) for k, v in sc['var_ratio'].items() if v}))
    print('  振幅自相关(1/2/5/10/20/60根):', [round(v, 3) for v in sc['amp_acf'].values()])
    print('  日振幅自相关(1/5/20日):', [round(v, 3) for v in sc['day_acf'].values()])
    print('  高振幅态自相关(1/5/30/120根):', [round(x, 3) for x in sc['hi_state_acf']])
    c = r['cond']
    print('[D7 条件] 各态下一根振幅档集中度(max):', ' '.join(
        f'{TRIG[STATES[i]]}{v*100:.0f}%' for i, v in enumerate(c['row_max_amp'])))
    print('  条件分布JS: 综卦对=%.4f vs 错卦对=%.4f' % (c['js_zong_mean'], c['js_cuo_mean']))
t = RES['tail']
print('\n[尾部相依]', {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in t['quantiles'].items()})
print('  尾部领先滞后(joint@95%):', {k: round(v, 5) for k, v in t['lead'].items()})
print('\nsaved results_dimensions.json')
