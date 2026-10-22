# -*- coding: utf-8 -*-
"""矩阵/数组视角分析：把六位编码数据放进各种矩阵结构里，看能读出什么信息。"""
import pandas as pd, numpy as np, json
from scipy.cluster.hierarchy import linkage, leaves_list

OUT = '.'
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
SIDX = {s: i for i, s in enumerate(STATES)}
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
# 综卦(上下颠倒=影线换边)对 / 错卦(三爻全反=K线完全反向)对
ZONG = [(2, 1), (5, 6)]                    # 震<->艮, 巽<->兑
CUO = [(3, 4), (0, 7), (2, 5), (1, 6)]     # 乾<->坤, 离<->坎, 震<->巽, 艮<->兑


def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, '01', '00')
    up = np.where(h > np.maximum(o, c), '01', '00')
    lo = np.where(l < np.minimum(o, c), '01', '00')
    return np.char.add(np.char.add(body, up), lo)


def load(sym):
    d0 = pd.read_csv(f'{sym}_2020_1m_raw.csv', parse_dates=['datetime'])
    d1 = pd.read_csv(f'{sym}_2021_2025_1m_raw.csv', parse_dates=['datetime'])
    d = pd.concat([d0, d1], ignore_index=True).sort_values('datetime').reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    rng = h - l
    safe = np.where(rng > 0, rng, 1.0)
    d['state'] = np.array([SIDX[x] for x in encode_ohlc(o, h, l, c)])
    d['chg'] = (c - o) / o * 100.0
    d['amp'] = rng / o * 100.0
    d['clv'] = np.where(rng > 0, (c - l) / safe, 0.5)
    d['up_sh'] = np.where(rng > 0, (h - np.maximum(o, c)) / safe, 0.0)
    d['lo_sh'] = np.where(rng > 0, (np.minimum(o, c) - l) / safe, 0.0)
    gap = d.datetime.diff().dt.total_seconds().values
    d['adj'] = np.concatenate([[False], gap[1:] == 60.0])
    d['year'] = d.datetime.dt.year.values
    d['td'] = (d.datetime + pd.to_timedelta((d.datetime.dt.hour >= 21).astype(int), unit='D')).dt.date.values
    return d


def mi_bits(x, y, nx, ny):
    H = np.zeros((nx, ny))
    np.add.at(H, (x, y), 1)
    P = H / len(x)
    px = P.sum(1, keepdims=True); py = P.sum(0, keepdims=True)
    nz = P > 0
    return float((P[nz] * np.log2(P[nz] / (px @ py)[nz])).sum())


def qbin(x, k):
    qs = np.quantile(x, [(i + 1) / k for i in range(k - 1)])
    return np.clip(np.digitize(x, qs), 0, k - 1)


def run_lengths(st, adj):
    runs = []
    cur = st[0]; ln = 1
    for i in range(1, len(st)):
        if adj[i] and st[i] == cur:
            ln += 1
        else:
            runs.append((int(cur), ln)); cur = st[i]; ln = 1
    runs.append((int(cur), ln))
    return runs


def analyze_sym(d, sym):
    n = len(d)
    st, chg, amp, clv = d.state.values, d.chg.values, d.amp.values, d.clv.values
    up_sh, lo_sh, adj, yr = d.up_sh.values, d.lo_sh.values, d.adj.values, d.year.values
    body3 = np.where(chg > 0, 2, np.where(chg == 0, 1, 0))
    has_up = (up_sh > 1e-12).astype(int)
    has_lo = (lo_sh > 1e-12).astype(int)
    R = {'sym': sym, 'n': int(n)}

    # ---------- ① 8x8 转移矩阵 + 谱分析 ----------
    su, sn = st[:-1][adj[1:]], st[1:][adj[1:]]
    cmk = np.zeros((8, 8), int); np.add.at(cmk, (su, sn), 1)
    P = cmk / np.maximum(cmk.sum(1, keepdims=True), 1)
    w, V = np.linalg.eig(P.T)
    i1 = np.argmax(w.real)
    pi = np.real(V[:, i1]); pi = np.abs(pi); pi /= pi.sum()
    lam = np.sort(np.abs(w))[::-1]
    gap_sp = 1 - lam[1]
    sojourn = 1.0 / np.maximum(1 - np.diag(P), 1e-9)
    rowH = -(P * np.log2(np.maximum(P, 1e-12))).sum(1)
    # 经验平稳分布 vs 理论
    emp = np.bincount(st, minlength=8) / n
    # 运行长度（数组视角的 run-length encoding）验证驻留时间
    runs = run_lengths(st, adj)
    obs_run = np.zeros(8); cnt_run = np.zeros(8)
    for s, ln in runs:
        obs_run[s] += ln; cnt_run[s] += 1
    obs_run /= np.maximum(cnt_run, 1)
    R['trans'] = {
        'count': cmk.tolist(), 'P': P.tolist(),
        'pi_theory': pi.tolist(), 'pi_emp': emp.tolist(),
        'pi_maxdiff': float(np.abs(pi - emp).max()),
        'eig_abs': [float(x) for x in lam], 'spectral_gap': float(gap_sp),
        'mixing_bars': float(1.0 / gap_sp),
        'sojourn_theory': sojourn.tolist(), 'sojourn_obs': obs_run.tolist(),
        'self_trans': np.diag(P).tolist(),
        'row_entropy': rowH.tolist()}

    # ---------- ② 跨年转移张量 (year x 8 x 8) 稳定性 ----------
    Pys = {}
    for y in np.unique(yr):
        m = adj[1:] & (yr[:-1] == y)
        cy = np.zeros((8, 8), int); np.add.at(cy, (st[:-1][m], st[1:][m]), 1)
        Pys[int(y)] = cy / np.maximum(cy.sum(1, keepdims=True), 1)
    fro = {int(y): float(np.linalg.norm(Pys[y] - P) / np.linalg.norm(P)) for y in Pys}
    R['trans_tensor'] = {'Fro_rel': fro}

    # ---------- ③ 字段互信息矩阵 (6x6) + 预测性 MI ----------
    clv8 = np.clip((clv * 8).astype(int), 0, 7)
    chg5, amp5 = qbin(chg, 5), qbin(amp, 5)
    F = {'实体3态': body3, '上影': has_up, '下影': has_lo, 'CLV8': clv8, '涨跌幅5档': chg5, '振幅5档': amp5}
    NF = {'实体3态': 3, '上影': 2, '下影': 2, 'CLV8': 8, '涨跌幅5档': 5, '振幅5档': 5}
    keys = list(F.keys())
    MI = np.zeros((6, 6))
    for i in range(6):
        for j in range(i, 6):
            v = mi_bits(F[keys[i]], F[keys[j]], NF[keys[i]], NF[keys[j]])
            MI[i, j] = MI[j, i] = v
    nxt_chg = chg[1:][adj[1:]]; nxt_amp = amp[1:][adj[1:]]
    t_dir = qbin(nxt_chg, 4); t_amp = qbin(nxt_amp, 4)
    pred = {}
    for k in keys:
        fk = F[k][:-1][adj[1:]]
        pred[k] = {'dir': mi_bits(fk, t_dir, NF[k], 4), 'amp': mi_bits(fk, t_amp, NF[k], 4)}
    pred['形态8态'] = {'dir': mi_bits(su, t_dir, 8, 4), 'amp': mi_bits(su, t_amp, 8, 4)}
    pred['code7(新码)'] = None  # 占位, 由 step17 结果引用
    R['mi'] = {'keys': keys, 'matrix': MI.tolist(), 'pred': pred}

    # ---------- ④ 相关矩阵: 域内 5x5 + 跨期 5(prev)x3(next) ----------
    W = np.column_stack([chg, amp, clv, up_sh, lo_sh])
    C_in = np.corrcoef(W.T)
    prev = W[:-1][adj[1:]]
    nxt3 = np.column_stack([nxt_chg, nxt_amp, clv[1:][adj[1:]]])
    C_cross = np.zeros((5, 3))
    for i in range(5):
        for j in range(3):
            C_cross[i, j] = np.corrcoef(prev[:, i], nxt3[:, j])[0, 1]
    R['corr'] = {'within': C_in.tolist(), 'cross': C_cross.tolist()}

    # ---------- ⑤ 状态距离矩阵 + 层次聚类 ----------
    Fs = np.zeros((8, 5))
    for i in range(8):
        m = st == i
        Fs[i] = [chg[m].mean(), amp[m].mean(), clv[m].mean(), up_sh[m].mean(), lo_sh[m].mean()]
    Fz = (Fs - Fs.mean(0)) / np.maximum(Fs.std(0, ddof=1), 1e-12)
    Dm = np.sqrt(((Fz[:, None, :] - Fz[None, :, :]) ** 2).sum(-1))
    Z = linkage(Dm[np.triu_indices(8, 1)], 'average')
    order = leaves_list(Z)
    allp = Dm[np.triu_indices(8, 1)]
    d_zong = np.mean([Dm[i, j] for i, j in ZONG])
    d_cuo = np.mean([Dm[i, j] for i, j in CUO])
    d_rest = np.mean([Dm[i, j] for i, j in np.argwhere(np.triu(np.ones((8, 8)), 1) > 0)
                      if (i, j) not in ZONG and (i, j) not in CUO and (j, i) not in ZONG and (j, i) not in CUO])
    R['dist'] = {'feat': Fs.tolist(), 'D': Dm.tolist(), 'order': order.tolist(),
                 'd_zong': float(d_zong), 'd_cuo': float(d_cuo), 'd_other': float(d_rest),
                 'd_mean': float(allp.mean())}

    # ---------- ⑥ 形状向量 PCA ----------
    rng = np.random.default_rng(7)
    idx = rng.choice(n, size=min(300000, n), replace=False)
    X = W[idx]
    Xz = (X - X.mean(0)) / X.std(0, ddof=1)
    Ux, Sx, Vt = np.linalg.svd(Xz, full_matrices=False)
    evr = Sx ** 2 / (Sx ** 2).sum()
    R['pca'] = {'evr': [float(x) for x in evr], 'loadings': Vt[:3].tolist(),
                'cum2': float(evr[:2].sum()), 'cum3': float(evr[:3].sum())}

    # ---------- ⑦ 日 x 时段 (345) 矩阵 SVD ----------
    gap = d.datetime.diff().dt.total_seconds().values
    seg = np.concatenate([[0], np.cumsum(gap[1:] != 60.0)])
    d = d.copy(); d['seg'] = seg
    d['seq'] = d.groupby('seg').cumcount().values
    seg_hour = d.groupby('seg').datetime.first().dt.hour
    BASE = {9: 0, 10: 75, 13: 135, 21: 225}
    slot = d.seq + d.seg.map(seg_hour).map(BASE).values
    ok = ~np.isnan(slot)
    tdc = pd.factorize(d.td)[0]
    nd = int(tdc.max()) + 1
    M = np.full((nd, 345), np.nan)
    M[tdc[ok], slot[ok].astype(int)] = d.amp.values[ok]
    M0 = np.nan_to_num(M)
    Um, Sm, Vtm = np.linalg.svd(M0, full_matrices=False)
    p = Sm ** 2 / (Sm ** 2).sum()
    eff_rank = float(np.exp(-(p * np.log(np.maximum(p, 1e-12))).sum()))
    # 日内轮廓: 按5分钟块平滑的前3个右奇异向量 (符号定向为正)
    prof = []
    for k in range(3):
        v = Vtm[k].copy()
        if v.mean() < 0:
            v = -v
        prof.append([float(v[i:i + 5].mean()) for i in range(0, 345, 5)])
    # 日载荷 vs 当日/次日收益
    o, c = d.open.values, d.close.values
    g = pd.DataFrame({'tdc': tdc, 'o': o, 'c': c, 'amp': amp, 'chg': chg}).groupby('tdc')
    dly = g.agg(o=('o', 'first'), c=('c', 'last'), amp=('amp', 'mean'), chg=('chg', 'mean'))
    dly['ret'] = dly.c.pct_change() * 100
    dly['next_ret'] = dly.ret.shift(-1)
    dly['next_amp'] = dly.amp.shift(-1)
    score = Um[:, 0] * Sm[0]
    if np.corrcoef(score, dly.amp.values)[0, 1] < 0:
        score = -score                      # 符号定向: 高载荷=高波动日
    sc = pd.Series(score)
    corr_load = {
        'ret': float(sc.corr(dly.ret)),
        'amp': float(sc.corr(dly.amp)),
        'next_amp': float(sc.iloc[:-1].corr(dly.next_amp.iloc[:-1]))}
    # 日 x 形态占用矩阵 SVD
    Cc = np.zeros((nd, 8)); np.add.at(Cc, (tdc, st), 1)
    Cn = Cc / np.maximum(Cc.sum(1, keepdims=True), 1)
    Uc, Sc, Vtc = np.linalg.svd(Cn - Cn.mean(0), full_matrices=False)
    f1 = Uc[:, 0] * Sc[0]
    f2 = Uc[:, 1] * Sc[1]
    R['daymat'] = {
        'n_days': nd, 'evr_top': [float(x) for x in p[:6]], 'eff_rank': eff_rank,
        'profile3': prof, 'load_corr': corr_load,
        'state_evr_top': [float(x) for x in (Sc ** 2 / (Sc ** 2).sum())[:4]],
        'f1_next_ret': float(np.corrcoef(f1[:-1], dly.next_ret.values[:-1])[0, 1]),
        'f2_next_ret': float(np.corrcoef(f2[:-1], dly.next_ret.values[:-1])[0, 1]),
        'f1_next_amp': float(np.corrcoef(f1[:-1], dly.next_amp.values[:-1])[0, 1])}

    # ---------- ⑧ 年 x 形态占用矩阵 ----------
    occ = np.zeros((len(np.unique(yr)), 8))
    for i, y in enumerate(np.unique(yr)):
        m = yr == y
        occ[i] = np.bincount(st[m], minlength=8) / m.sum()
    R['year_occ'] = {'years': [int(y) for y in np.unique(yr)], 'occ': occ.tolist()}
    return R


RES = {}
for sym in ['MA', 'EG']:
    d = load(sym)
    RES[sym] = analyze_sym(d, sym)
    print(f'===== {sym} 完成 n={RES[sym]["n"]:,} =====')

# ---------- ⑨ MA x EG 交叉相关 / 联合占用 ----------
dMA, dEG = load('MA'), load('EG')
mm = pd.merge(
    dMA[['datetime', 'state', 'chg', 'amp']].rename(columns={'state': 'st_MA', 'chg': 'chg_MA', 'amp': 'amp_MA'}),
    dEG[['datetime', 'state', 'chg', 'amp']].rename(columns={'state': 'st_EG', 'chg': 'chg_EG', 'amp': 'amp_EG'}),
    on='datetime', how='inner')
a, b = mm.chg_MA.values, mm.chg_EG.values
aa, ab = mm.amp_MA.values, mm.amp_EG.values
n = len(a)
lead = {'chg': {}, 'amp': {}}
for k in range(-10, 11):
    if k >= 0:
        lead['chg'][k] = float(np.corrcoef(a[:n - k], b[k:])[0, 1]) if k < n else None
        lead['amp'][k] = float(np.corrcoef(aa[:n - k], ab[k:])[0, 1]) if k < n else None
    else:
        lead['chg'][k] = float(np.corrcoef(a[-k:], b[:n + k])[0, 1])
        lead['amp'][k] = float(np.corrcoef(aa[-k:], ab[:n + k])[0, 1])
J = np.zeros((8, 8), int); np.add.at(J, (mm.st_MA.values, mm.st_EG.values), 1)
mi_joint = mi_bits(mm.st_MA.values, mm.st_EG.values, 8, 8)
# 置换检验
rng = np.random.default_rng(7)
null = []
for _ in range(20):
    null.append(mi_bits(mm.st_MA.values, rng.permutation(mm.st_EG.values), 8, 8))
RES['cross'] = {
    'n_common': int(n), 'lead': lead,
    'joint': J.tolist(), 'joint_MI': float(mi_joint),
    'joint_MI_null': float(np.mean(null)), 'joint_MI_p': float((np.sum(np.array(null) >= mi_joint) + 1) / 21),
    'corr_chg': float(np.corrcoef(a, b)[0, 1]), 'corr_amp': float(np.corrcoef(aa, ab)[0, 1])}
Jp = J / J.sum()
top_pairs = sorted([(Jp[i, j], STATES[i], STATES[j]) for i in range(8) for j in range(8)], reverse=True)[:5]
RES['cross']['top_pairs'] = [[float(p), a_, b_] for p, a_, b_ in top_pairs]
print('cross MI joint = %.4f bits (null %.4f, p=%.3f)' % (mi_joint, np.mean(null), RES['cross']['joint_MI_p']))
print('lead argmax: chg@k=%s amp@k=%s' % (
    max(lead['chg'], key=lambda k: abs(lead['chg'][k])), max(lead['amp'], key=lambda k: abs(lead['amp'][k]))))
print('top joint pairs:', [(f'{p*100:.1f}%', a_ + TRIG[a_], b_ + TRIG[b_]) for p, a_, b_ in top_pairs])

json.dump(RES, open(f'{OUT}/results_matrix.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ---------- 摘要打印 ----------
for sym in ['MA', 'EG']:
    r = RES[sym]
    t = r['trans']
    print(f'\n===== {sym} 矩阵视角摘要 =====')
    print('[转移矩阵] 平稳分布(理论):', ' '.join(f'{TRIG[s]}{v*100:.1f}%' for s, v in zip(STATES, t['pi_theory'])))
    print('  经验分布:              ', ' '.join(f'{v*100:.1f}%' for v in t['pi_emp']))
    print('  |特征值| top5:', [round(x, 3) for x in t['eig_abs'][:5]],
          ' 谱间隙=%.4f 混合时间=%.1f根' % (t['spectral_gap'], t['mixing_bars']))
    print('  平稳vs经验最大差=%.4f  自转移概率:' % t['pi_maxdiff'],
          ' '.join(f'{TRIG[s]}{v*100:.0f}%' for s, v in zip(STATES, t['self_trans'])))
    print('  驻留(理论 vs 实测):', ' '.join(f'{TRIG[s]}:{a:.1f}/{b:.1f}' for s, a, b in zip(STATES, t['sojourn_theory'], t['sojourn_obs'])))
    print('  跨年Fro相对差:', {k: round(v, 3) for k, v in r['trans_tensor']['Fro_rel'].items()})
    mi = r['mi']
    print('[MI矩阵] 对角外最大:', sorted([(round(mi['matrix'][i][j], 3), mi['keys'][i], mi['keys'][j])
          for i in range(6) for j in range(i + 1, 6)], reverse=True)[:4])
    print('  预测MI(方向/振幅) 比特:', {k: (round(v['dir'], 4), round(v['amp'], 4)) for k, v in mi['pred'].items() if v})
    print('[相关] within diag附近 |cross| top3:',
          sorted([(round(abs(r['corr']['cross'][i][j]), 3), ['chg', 'amp', 'clv', 'up', 'lo'][i], ['next_chg', 'next_amp', 'next_clv'][j])
                  for i in range(5) for j in range(3)], reverse=True)[:3])
    d = r['dist']
    print('[距离] 均值=%.3f 综卦对=%.3f 错卦对=%.3f 其他对=%.3f' % (d['d_mean'], d['d_zong'], d['d_cuo'], d['d_other']))
    print('  聚类顺序:', [STATES[i] + TRIG[STATES[i]] for i in d['order']])
    p = r['pca']
    print('[PCA] 累计方差: PC1=%.3f PC1+2=%.3f PC1-3=%.3f' % (p['evr'][0], p['cum2'], p['cum3']))
    print('  PC1载荷(chg,amp,clv,up,lo):', [round(x, 2) for x in p['loadings'][0]])
    print('  PC2载荷:', [round(x, 2) for x in p['loadings'][1]])
    print('  PC3载荷:', [round(x, 2) for x in p['loadings'][2]])
    dm = r['daymat']
    print('[日x时段] 有效秩=%.2f 前3奇异值能量=%s' % (dm['eff_rank'], [round(x, 3) for x in dm['evr_top'][:3]]))
    print('  日载荷相关: 当日ret=%.3f 当日amp=%.3f 次日amp=%.3f' % (dm['load_corr']['ret'], dm['load_corr']['amp'], dm['load_corr']['next_amp']))
    print('  占用矩阵SVD: evr=%s f1~次日ret=%.3f f1~次日amp=%.3f' % ([round(x, 3) for x in dm['state_evr_top']], dm['f1_next_ret'], dm['f1_next_amp']))
    print('[年占用] 平盘率(000000+010000? no: body3=1) by year:')
    yo = r['year_occ']
    for y, row in zip(yo['years'], yo['occ']):
        flat = row[0] + row[4]  # 000000(跌平) + 010000(涨平) 近似平盘占比
        print(f'    {y}: 平盘态合计={flat*100:.1f}%  坤(000101)={row[3]*100:.1f}%  乾(010000)={row[4]*100:.1f}%')
print('\nsaved results_matrix.json')
