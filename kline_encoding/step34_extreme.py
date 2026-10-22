# -*- coding: utf-8 -*-
"""Step 34: 极端行情专题——六位编码与 HMM 在压力体制下的表现
窗口：CRASH20(疫情+油价战) / BOUNCE20(V反+OPEC) / BULL21(能耗双控挤仓) /
      TARIFF25(关税崩盘) / SQUEEZE26(春季挤仓与崩塌) / CALM24(平静基线) / TRAIN(全参考)
指标：状态分布漂移、转移矩阵稳定性、持续期、clv 均值回归、日内剖面、
      HMM 隐态解码（用 step28 训练参数）、共尾压力倍数（train 阈值 + slot 调整基准）
"""
import sys; sys.path.insert(0, '.')
import numpy as np, pandas as pd
from scipy.special import logsumexp
from ke_common import load, STATES, TRIG, adj_pairs, spearman

WIN = {
    'CRASH20': ('2020-02-03', '2020-03-31'),
    'BOUNCE20': ('2020-04-01', '2020-04-30'),
    'BULL21': ('2021-09-01', '2021-11-30'),
    'TARIFF25': ('2025-04-01', '2025-04-30'),
    'SQUEEZE26': ('2026-03-01', '2026-04-30'),
    'CALM24': ('2024-01-01', '2024-12-31'),
    'TRAIN': ('2020-01-01', '2025-12-31'),
}
PARTS = ['2020', '2021_2025', '2026']
AMP_FLOOR = {}   # 每品种非零最小振幅/2（与 step28 一致）


def H(p):
    p = np.asarray(p, float); p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


def hmm_filter_batch(logx, pi, A, mu, sd):
    """logx: list of 等长 1D 数组 -> gamma (list of (L,K))"""
    X = np.vstack(logx)
    B, L = X.shape
    K = len(pi)
    E = -0.5 * ((X[:, :, None] - mu[None, None, :]) ** 2) / (sd[None, None, :] ** 2) \
        - np.log(sd)[None, None, :] - 0.5 * np.log(2 * np.pi)
    logA = np.log(A); logpi = np.log(pi)
    alpha = np.empty((B, L, K))
    a = logpi[None, :] + E[:, 0, :]
    alpha[:, 0, :] = np.exp(a - logsumexp(a, axis=1, keepdims=True))
    for t in range(1, L):
        tmp = np.log(alpha[:, t - 1, :])[:, :, None] + logA[None, :, :]
        a = logsumexp(tmp, axis=1) + E[:, t, :]
        alpha[:, t, :] = np.exp(a - logsumexp(a, axis=1, keepdims=True))
    return alpha / alpha.sum(2, keepdims=True)


def run_lengths(states, adj):
    """沿 adj 链的 run 长度；返回 (run_len, start_idx, end_idx)"""
    n = len(states)
    starts = [0]
    for i in range(1, n):
        if (not adj[i]) or states[i] != states[i - 1]:
            starts.append(i)
    starts.append(n)
    out = []
    for a, b in zip(starts[:-1], starts[1:]):
        out.append((b - a, a, b - 1))
    return out


D = {}
for sym in ('MA', 'EG'):
    d = load(sym, PARTS)
    n = len(d)
    dt = pd.to_datetime(d.datetime)
    td = pd.Series(d.td.values)
    td_dt = pd.to_datetime(td)
    amp = d.amp.values
    absr = np.abs(d.logret.values)
    st = d.state.values
    adj = d.adj.values
    clv = d.clv.values
    lr = d.logret.values
    slot = d.slot.values
    a_pos = amp[amp > 0]
    AMP_FLOOR[sym] = float(a_pos.min() / 2.0)

    # 预计算：全序列 run、转移对、HMM 分组
    runs = run_lengths(st, adj)
    run_start_td = td_dt.iloc[[r[1] for r in runs]].values
    s_t, s_t1, idx1 = adj_pairs(d)           # 相邻对
    pair_td = td_dt.iloc[idx1].values
    # HMM：按会话等长分组批处理 forward 滤波
    hp = __import__('json').load(open('results_persist.json', encoding='utf-8'))[sym]['hidden']
    segv = d.seg.values
    gamma_full = np.full((n, 3), np.nan)
    segs = d.groupby('seg').indices
    by_len = {}
    for s, idxs in segs.items():
        by_len.setdefault(len(idxs), []).append(np.asarray(idxs))
    for L, groups in by_len.items():
        logx = [np.log(np.maximum(amp[g], AMP_FLOOR[sym])) for g in groups]
        gm = hmm_filter_batch(logx, np.array(hp['pi']), np.array(hp['A']),
                              np.array(hp['mu_log']), np.array(hp['sd_log']))
        for g, gg in zip(groups, gm):
            gamma_full[g, :] = gg
    map_state = gamma_full.argmax(1)

    R = {}
    # 参考：train 阈值（共尾压力用）
    mtr = td_dt.dt.year.values <= 2025
    thr99 = float(np.quantile(amp[mtr], 0.99))
    # slot 条件独立基准（train）
    pA = np.array([ (amp[mtr & (slot == s)] > thr99).mean() if (mtr & (slot == s)).sum() > 100 else 0.0 for s in range(345)])
    R['_thr99'] = thr99
    R['_pA_by_slot'] = [round(float(x), 6) for x in pA]

    for wname, (d0, d1) in WIN.items():
        m = (td_dt >= pd.Timestamp(d0)) & (td_dt <= pd.Timestamp(d1))
        nb = int(m.sum())
        w = {'n_bars': nb, 'n_td': int(td[m].nunique()),
             'mean_amp': float(amp[m].mean()), 'mean_absr_bp': float(absr[m].mean() * 1e4),
             'p95_amp': float(np.quantile(amp[m], 0.95))}
        # 状态分布
        cnt = np.bincount(st[m], minlength=8) / max(nb, 1)
        w['state_freq'] = [round(float(x), 4) for x in cnt]
        w['H_state'] = H(cnt)
        # 转移矩阵（窗口内相邻对）
        pm = (pair_td >= pd.Timestamp(d0)) & (pair_td <= pd.Timestamp(d1))
        Cm = np.zeros((8, 8))
        np.add.at(Cm, (s_t[pm], s_t1[pm]), 1)
        rs = Cm.sum(1, keepdims=True)
        P = np.divide(Cm, rs, out=np.zeros_like(Cm), where=rs > 0)
        w['trans_diag'] = float(np.nanmean(np.diag(P)[rs[:, 0] > 0]))
        w['trans_rowH'] = float(np.mean([H(P[i]) for i in range(8) if rs[i, 0] > 0]))
        w['trans_counts'] = Cm.astype(int).tolist()
        # 持续期（窗口内开始的 run）
        rm = (run_start_td >= pd.Timestamp(d0)) & (run_start_td <= pd.Timestamp(d1))
        lens = np.array([r[0] for r, k in zip(runs, rm) if k])
        w['n_runs'] = int(len(lens))
        w['mean_run'] = float(lens.mean()) if len(lens) else None
        w['long_run_share'] = float((lens >= 5).mean()) if len(lens) else None
        # clv 均值回归（窗口内相邻对：clv(t) → logret(t+1)）
        cm = pm & np.isfinite(clv[idx1 - 1]) & np.isfinite(lr[idx1])
        w['ic_clv_rc'] = spearman(clv[idx1 - 1][cm], lr[idx1][cm]) if cm.sum() > 50 else None
        # 日内剖面（日盘 slots 0-224）
        for lbl, sm in (('first15', (slot >= 0) & (slot < 15)), ('mid', (slot >= 75) & (slot < 225)),
                        ('last15', (slot >= 330))):
            mm = m & sm
            w[f'absr_{lbl}_bp'] = float(absr[mm].mean() * 1e4) if mm.sum() > 30 else None
        # HMM 解码
        gm_ = gamma_full[m]
        if np.isfinite(gm_).all() and nb > 100:
            w['hmm_occ'] = [round(float(x), 4) for x in gm_.mean(0)]
            ms = map_state[m]
            sw = int((ms[1:] != ms[:-1]).sum())
            w['hmm_switches_per_1000'] = round(sw / nb * 1000, 2)
            w['hmm_state_amp'] = [round(float(amp[m][ms == k].mean()), 4) if (ms == k).sum() > 30 else None
                                  for k in range(3)]
        R[wname] = w
        print(f'{sym} {wname}: {nb:,} bars, amp={w["mean_amp"]:.3f}%, H_state={w["H_state"]:.3f}, '
              f'diag={w["trans_diag"]:.3f}, mean_run={w["mean_run"]}, ic_clv={w["ic_clv_rc"]}')
    D[sym] = R

# ===== 共尾压力倍数（双品种联合）=====
maj, egj = D['MA'], D['EG']
print('\n=== 共尾压力（train q=0.99 阈值）===')
cotail = {}
for wname, (d0, d1) in WIN.items():
    row = {}
    for sym in ('MA', 'EG'):
        d = load(sym, PARTS)
        td_dt = pd.to_datetime(pd.Series(d.td.values))
        m = (td_dt >= pd.Timestamp(d0)) & (td_dt <= pd.Timestamp(d1)).values
        row[sym] = (d.datetime.values[m], d.amp.values[m] > D[sym]['_thr99'], d.slot.values[m])
    # 对齐
    da = pd.Series(row['MA'][1], index=pd.to_datetime(row['MA'][0]))
    db = pd.Series(row['EG'][1], index=pd.to_datetime(row['EG'][0]))
    common = da.index.intersection(db.index)
    ja, jb = da.loc[common].values, db.loc[common].values
    sa = row['MA'][2][pd.Index(row['MA'][0]).get_indexer(common)]
    pj = float((ja & jb).mean())
    # slot 调整基准（train 边际）
    pa_slot = np.array(D['MA']['_pA_by_slot'])[sa]
    pb_slot = np.array(D['EG']['_pA_by_slot'])[sa]
    base = float((pa_slot * pb_slot).mean())
    # 窗口内 slot 条件独立基准（窗口自身边际 + EB 收缩，兼容短窗口）
    sa_sum = np.zeros(345); sb_sum = np.zeros(345); ns_ = np.zeros(345)
    np.add.at(sa_sum, sa, ja.astype(float)); np.add.at(sb_sum, sa, jb.astype(float)); np.add.at(ns_, sa, 1.0)
    alpha = 10.0
    pa_win, pb_win = float(ja.mean()), float(jb.mean())
    pa_w = (sa_sum + alpha * pa_win) / (ns_ + alpha)
    pb_w = (sb_sum + alpha * pb_win) / (ns_ + alpha)
    base_win = float((pa_w[sa] * pb_w[sa]).mean())
    naive = float(ja.mean() * jb.mean())
    row_out = {'n': int(len(common)), 'p_joint': pj, 'base_slot': base,
               'mult_slot': pj / base if base > 0 else None, 'mult_naive': pj / naive if naive > 0 else None,
               'base_win': base_win, 'mult_win': pj / base_win if base_win > 0 else None}
    cotail[wname] = row_out
    print(f'{wname}: n={row_out["n"]:,} p_joint={pj:.5f} | train基准 {row_out["mult_slot"]:.2f}x '
          f'朴素 {row_out["mult_naive"]:.2f}x 窗口内基准 {row_out["mult_win"]:.2f}x')

import json
json.dump({'windows': D, 'cotail': cotail}, open('results_extreme.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
print('saved results_extreme.json')
