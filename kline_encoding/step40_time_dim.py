# -*- coding: utf-8 -*-
"""step40: 时间维度入模实验
问题：六位编码不含时间维度。若把「何时交易」（slot）作为显式条件加入，预测下一根
振幅五档 / 方向，能多拿到多少信息（bit）？增量集中在哪些时段？
阶梯模型（train 标定，EB 收缩，holdout 评估）。所有条件变量一律取 t-1：
  幅度任务: marg -> +slot -> 链(前状态) -> 链×slot -> 前波幅 -> 前波幅×slot -> 链×前波幅 -> 全件
  方向任务: marg -> +slot -> clv五档(前根) -> clv×slot -> 前状态 -> 前状态×slot
"""
import numpy as np, pandas as pd, json
from ke_common import load, spearman

ALPHA = 40.0
ALPHA_XS = 120.0        # 细条件（slot 参与）用更大收缩
PARTS = ['2020', '2021_2025', '2026']
SYMS = ['MA', 'EG', 'TA']

BLOCKS = [
    ('09:00-09:14 开盘', 0, 15), ('09:15-10:15', 15, 75), ('10:30-11:30', 75, 135),
    ('13:30-14:44', 135, 210), ('14:45-15:00 收盘', 210, 225),
    ('21:00-21:14 夜开', 225, 240), ('21:15-22:59 夜中', 240, 330), ('23:00 夜收', 330, 345),
]
NB = len(BLOCKS)

def block_of(slot):
    i = np.full(slot.shape, NB - 1, dtype=int)
    for j, (_, a, b) in enumerate(BLOCKS):
        i[(slot >= a) & (slot < b)] = j
    return i

def eb(cnt, prior, alpha):
    pr = np.broadcast_to(prior, cnt.shape)
    return (cnt + alpha * pr) / (cnt.sum(-1, keepdims=True) + alpha)

def nll_of(p, tgt):
    return float(-np.mean(np.log2(np.clip(p[np.arange(len(tgt)), tgt], 1e-12, 1))))

out = {}
for sym in SYMS:
    d = load(sym, PARTS)
    yr = d.year.values
    amp = d.amp.values
    qs = np.quantile(amp[yr <= 2025], [0.2, 0.4, 0.6, 0.8])
    cls = np.clip(np.digitize(amp, qs), 0, 4)
    s8 = d.state.values
    slot = d.slot.values.astype(int)
    blk = block_of(slot)
    adj = d.adj.values
    seq = d.seq.values
    lr = d.logret.values
    clv = d.clv.values

    # 条件变量一律取 t-1（成对 mask 保证同会话相邻）
    s8p = np.concatenate([[s8[0]], s8[:-1]])
    clsp = np.concatenate([[cls[0]], cls[:-1]])     # 前一根振幅档 = 「前波幅」
    clvp = np.concatenate([[clv[0]], clv[:-1]])

    valid = adj & (seq > 0) & (np.concatenate([[False], adj[:-1]]))
    mtr = valid & (yr <= 2025)
    ho = valid & (yr == 2026)
    it, ih = np.where(mtr)[0], np.where(ho)[0]

    # ---------- 幅度任务 ----------
    K = 5
    p_marg = np.bincount(cls[mtr], minlength=K) + 1e-8
    p_marg = p_marg / p_marg.sum()

    cnt_slot = np.zeros((345, K)); np.add.at(cnt_slot, (slot[mtr], cls[mtr]), 1.0)
    P_slot = eb(cnt_slot, p_marg, ALPHA)
    cnt_ch = np.zeros((8, K)); np.add.at(cnt_ch, (s8p[mtr], cls[mtr]), 1.0)
    P_ch = eb(cnt_ch, p_marg, ALPHA)
    cnt_chs = np.zeros((8, 345, K)); np.add.at(cnt_chs, (s8p[mtr], slot[mtr], cls[mtr]), 1.0)
    P_chs = eb(cnt_chs, P_slot[None, :, :], ALPHA_XS)
    cnt_v = np.zeros((K, K)); np.add.at(cnt_v, (clsp[mtr], cls[mtr]), 1.0)
    P_v = eb(cnt_v, p_marg, ALPHA)
    cnt_vs = np.zeros((K, 345, K)); np.add.at(cnt_vs, (clsp[mtr], slot[mtr], cls[mtr]), 1.0)
    P_vs = eb(cnt_vs, P_slot[None, :, :], ALPHA_XS)
    cnt_cv = np.zeros((8, K, K)); np.add.at(cnt_cv, (s8p[mtr], clsp[mtr], cls[mtr]), 1.0)
    P_cv = eb(cnt_cv, P_v[None, :, :], ALPHA_XS)
    cnt_cvs = np.zeros((8, K, 345, K)); np.add.at(cnt_cvs, (s8p[mtr], clsp[mtr], slot[mtr], cls[mtr]), 1.0)
    P_cvs = eb(cnt_cvs, P_cv[:, :, None, :], ALPHA_XS * 2)

    def evalp(fn, tgt):
        return nll_of(fn(it), tgt[it]), nll_of(fn(ih), tgt[ih])

    res_amp = {}
    res_amp['marg'] = evalp(lambda m: np.tile(p_marg, (len(m), 1)), cls)
    res_amp['slot'] = evalp(lambda m: P_slot[slot[m]], cls)
    res_amp['chain1'] = evalp(lambda m: P_ch[s8p[m]], cls)
    res_amp['chain1_slot'] = evalp(lambda m: P_chs[s8p[m], slot[m]], cls)
    res_amp['vol1'] = evalp(lambda m: P_v[clsp[m]], cls)
    res_amp['vol1_slot'] = evalp(lambda m: P_vs[clsp[m], slot[m]], cls)
    res_amp['chain1_vol1'] = evalp(lambda m: P_cv[s8p[m], clsp[m]], cls)
    res_amp['full'] = evalp(lambda m: P_cvs[s8p[m], clsp[m], slot[m]], cls)

    # ---------- 方向任务 ----------
    up = (lr > 0).astype(int)
    K2 = 2
    p_dir = np.bincount(up[mtr], minlength=K2).astype(float)
    p_dir = p_dir / p_dir.sum()

    cnt_dslot = np.zeros((345, K2)); np.add.at(cnt_dslot, (slot[mtr], up[mtr]), 1.0)
    P_dslot = eb(cnt_dslot, p_dir, ALPHA)
    clvq = np.quantile(clv[mtr], [0.2, 0.4, 0.6, 0.8])
    clvc = np.clip(np.digitize(clvp, clvq), 0, 4)   # 前一根 clv 五档
    cnt_dc = np.zeros((5, K2)); np.add.at(cnt_dc, (clvc[mtr], up[mtr]), 1.0)
    P_dc = eb(cnt_dc, p_dir, ALPHA)
    cnt_dcs = np.zeros((5, 345, K2)); np.add.at(cnt_dcs, (clvc[mtr], slot[mtr], up[mtr]), 1.0)
    P_dcs = eb(cnt_dcs, P_dslot[None, :, :], ALPHA_XS)
    cnt_ds8 = np.zeros((8, K2)); np.add.at(cnt_ds8, (s8p[mtr], up[mtr]), 1.0)
    P_ds8 = eb(cnt_ds8, p_dir, ALPHA)
    cnt_ds8s = np.zeros((8, 345, K2)); np.add.at(cnt_ds8s, (s8p[mtr], slot[mtr], up[mtr]), 1.0)
    P_ds8s = eb(cnt_ds8s, P_dslot[None, :, :], ALPHA_XS)

    res_dir = {}
    res_dir['marg'] = evalp(lambda m: np.tile(p_dir, (len(m), 1)), up)
    res_dir['slot'] = evalp(lambda m: P_dslot[slot[m]], up)
    res_dir['clv5'] = evalp(lambda m: P_dc[clvc[m]], up)
    res_dir['clv5_slot'] = evalp(lambda m: P_dcs[clvc[m], slot[m]], up)
    res_dir['state1'] = evalp(lambda m: P_ds8[s8p[m]], up)
    res_dir['state1_slot'] = evalp(lambda m: P_ds8s[s8p[m], slot[m]], up)

    # ---------- 诊断：分块 ----------
    dg = {}
    icb = []
    for j, (nm, a, b) in enumerate(BLOCKS):
        m = mtr & (blk == j)
        if m.sum() > 500:
            icb.append({'block': nm, 'n': int(m.sum()),
                        'ic_clv': round(float(spearman(clvp[m], lr[m])), 4)})
        else:
            icb.append({'block': nm, 'n': int(m.sum()), 'ic_clv': None})
    dg['ic_clv_by_block'] = icb
    dg['amp_by_block'] = [{'block': nm, 'mean_amp_bp': round(float(np.mean(amp[mtr & (blk == j)]) * 100), 1),
                           'n': int((mtr & (blk == j)).sum())} for j, (nm, a, b) in enumerate(BLOCKS)]
    gain_map = []
    for j, (nm, a, b) in enumerate(BLOCKS):
        m = ho & (blk == j)
        if m.sum() > 200:
            ii = np.where(m)[0]
            n0 = nll_of(np.tile(p_marg, (len(ii), 1)), cls[ii])
            n1 = nll_of(P_slot[slot[ii]], cls[ii])
            gain_map.append({'block': nm, 'n_ho': int(m.sum()),
                             'nll_marg': round(n0, 4), 'nll_slot': round(n1, 4),
                             'gain_bits': round(n0 - n1, 4)})
    dg['slot_gain_map'] = gain_map
    T_all = np.zeros((8, 8)); np.add.at(T_all, (s8p[mtr], s8[mtr]), 1.0)
    T_all = T_all / T_all.sum(1, keepdims=True)
    tvs = []
    for j, (nm, a, b) in enumerate(BLOCKS):
        m = mtr & (blk == j)
        if m.sum() > 2000:
            T = np.zeros((8, 8)); np.add.at(T, (s8p[m], s8[m]), 1.0)
            T = T / T.sum(1, keepdims=True)
            tvs.append({'block': nm, 'tv': round(float(np.abs(T - T_all).sum(1).mean() / 2), 4), 'n': int(m.sum())})
    dg['chain_tv_by_block'] = tvs

    out[sym] = {'n_train': int(mtr.sum()), 'n_hold': int(ho.sum()),
                'amp': res_amp, 'dir': res_dir, 'diag': dg}
    print(f'== {sym} (train {mtr.sum():,}, hold {ho.sum():,})')
    for k, (a, b) in res_amp.items():
        print(f'  amp {k:12s} train {a:.4f}  hold {b:.4f}')
    for k, (a, b) in res_dir.items():
        print(f'  dir {k:12s} train {a:.4f}  hold {b:.4f}')
    for g in gain_map:
        print(f'  slot-gain {g["block"]:18s} {g["gain_bits"]:+.4f} bit (n={g["n_ho"]})')

json.dump(out, open('results_time_dim.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved results_time_dim.json')
