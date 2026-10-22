# -*- coding: utf-8 -*-
"""Step 35: 板块分析（15 品种主力连续）
A. 两两共尾矩阵：朴素 vs slot（分钟桶）条件独立基准，train 全窗 + holdout
B. 转移矩阵同质性：两两 8×8 距离 vs 同品种半分噪声底（z 分数）
C. 振幅相关矩阵：原始 + slot 去均值（纯共振）
D. 方向共尾：同向 vs 反向联合极端（slot 调整）
E. 每品种一阶/二阶链 vs 边际：holdout 下一根振幅五档 log-loss
注意：IF 无夜盘 09:30 开盘、SC 夜盘至 02:29、PX/PR/UR 历史短——统一用「日历分钟桶」对齐，不用 slot。
"""
import sys; sys.path.insert(0, '.')
import os
import numpy as np, pandas as pd
from ke_common import load, STATES, TRIG

PARTS = ['2020', '2021_2025', '2026']
SYMS = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'UR', 'FG', 'SA', 'PF', 'PX', 'PR', 'EB', 'SC', 'IF']
Q = 0.99

# ---------- 载入（跳过只有表头的空段，如 PX/PR 的 2020） ----------
def load_sk(sym):
    parts = [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
    return load(sym, parts)


D = {}
for sym in SYMS:
    d = load_sk(sym)
    dt = pd.to_datetime(d.datetime)
    D[sym] = {
        'dt': dt.values.astype('datetime64[m]'),
        'amp': d.amp.values,
        'lr': d.logret.values,
        'state': d.state.values,
        'adj': d.adj.values,
        'year': d.year.values,
        'mod': (dt.dt.hour.values * 60 + dt.dt.minute.values),
    }
    print(f'loaded {sym}: {len(d):,}')

NMOD = 1440


def align(sa, sb):
    """按 datetime 内连接，返回 (ia, ib) 位置索引"""
    da, db = D[sa]['dt'], D[sb]['dt']
    idx = np.searchsorted(da, db)
    idx[idx >= len(da)] = len(da) - 1
    m = da[idx] == db
    return idx[m], np.where(m)[0]


def pair_cotail(sa, sb, q=Q):
    ia, ib = align(sa, sb)
    A, B = D[sa], D[sb]
    amp_a, amp_b = A['amp'][ia], B['amp'][ib]
    yr = A['year'][ia]
    mtr, mte = yr <= 2025, yr == 2026
    tha = np.quantile(amp_a[mtr], q)
    thb = np.quantile(amp_b[mtr], q)
    ea, eb = amp_a > tha, amp_b > thb
    mod = A['mod'][ia]
    # slot 条件独立基准（train 分钟桶边际）
    pa_slot = np.zeros(NMOD); pb_slot = np.zeros(NMOD); ns = np.zeros(NMOD)
    np.add.at(pa_slot, mod[mtr], ea[mtr].astype(float))
    np.add.at(pb_slot, mod[mtr], eb[mtr].astype(float))
    np.add.at(ns, mod[mtr], 1.0)
    alpha = 10.0
    pa_w, pb_w = ea[mtr].mean(), eb[mtr].mean()
    pa_slot = (pa_slot + alpha * pa_w) / (ns + alpha)
    pb_slot = (pb_slot + alpha * pb_w) / (ns + alpha)
    base = float((pa_slot[mod] * pb_slot[mod]).mean())
    out = {'n': int(len(ia)), 'n_holdout': int(mte.sum()), 'tha': float(tha), 'thb': float(thb)}
    for lbl, m in (('train', mtr), ('holdout', mte)):
        pj = float((ea[m] & eb[m]).mean())
        out[f'p_joint_{lbl}'] = pj
        out[f'mult_naive_{lbl}'] = pj / max(float(ea[m].mean() * eb[m].mean()), 1e-12)
        out[f'mult_slot_{lbl}'] = pj / max(base, 1e-12)
    # 方向共尾（train，slot 调整）：同向 vs 反向
    sga = np.sign(A['lr'][ia]); sgb = np.sign(B['lr'][ib])
    same = (sga * sgb) > 0
    opp = (sga * sgb) < 0
    # 基准：slot 内同向/反向的边缘概率乘积
    ps_same = np.zeros(NMOD); po_same = np.zeros(NMOD); ps_opp = np.zeros(NMOD); po_opp = np.zeros(NMOD)
    np.add.at(ps_same, mod[mtr], (ea[mtr] & same[mtr]).astype(float))
    np.add.at(po_same, mod[mtr], (ea[mtr] & ~same[mtr]).astype(float))
    np.add.at(ps_opp, mod[mtr], (eb[mtr] & same[mtr]).astype(float))
    np.add.at(po_opp, mod[mtr], (eb[mtr] & ~same[mtr]).astype(float))
    ps_same = (ps_same + alpha * (ea[mtr] & same[mtr]).mean()) / (ns + alpha)
    po_same = (po_same + alpha * (ea[mtr] & ~same[mtr]).mean()) / (ns + alpha)
    ps_opp = (ps_opp + alpha * (eb[mtr] & same[mtr]).mean()) / (ns + alpha)
    po_opp = (po_opp + alpha * (eb[mtr] & ~same[mtr]).mean()) / (ns + alpha)
    pjs = float((ea[mtr] & eb[mtr] & same[mtr]).mean())
    pjo = float((ea[mtr] & eb[mtr] & opp[mtr]).mean())
    out['mult_same_dir'] = pjs / max(float((ps_same[mod] * ps_opp[mod]).mean()), 1e-12)
    out['mult_opp_dir'] = pjo / max(float((po_same[mod] * po_opp[mod]).mean()), 1e-12)
    return out


print('\n=== A/D. 共尾与方向矩阵 ===')
cotail = {}
for i, sa in enumerate(SYMS):
    for sb in SYMS[i + 1:]:
        cotail[f'{sa}-{sb}'] = pair_cotail(sa, sb)
        r = cotail[f'{sa}-{sb}']
        print(f"{sa}-{sb}: n={r['n']:,} 朴素 {r['mult_naive_train']:.1f}x slot {r['mult_slot_train']:.2f}x "
              f"(holdout slot {r['mult_slot_holdout']:.2f}x) 同向 {r['mult_same_dir']:.2f}x 反向 {r['mult_opp_dir']:.2f}x")

print('\n=== B. 转移矩阵同质性 ===')
# adj_pairs 需要 DataFrame；直接从 D 重建相邻对
def trans_matrix(sym):
    d = D[sym]
    st, adj = d['state'], d['adj']
    s_t = st[:-1][adj[1:]]
    s_t1 = st[1:][adj[1:]]
    m = d['year'][1:][adj[1:]] <= 2025
    C = np.zeros((8, 8))
    np.add.at(C, (s_t[m], s_t1[m]), 1)
    rs = C.sum(1, keepdims=True)
    return np.divide(C, rs, out=np.zeros_like(C), where=rs > 0), C

Pm = {}
for sym in SYMS:
    Pm[sym], C = trans_matrix(sym)
# 同品种半分噪声底
rng = np.random.default_rng(7)
self_d = []
for sym in SYMS:
    d = D[sym]
    st, adj = d['state'], d['adj']
    m = d['year'][1:][adj[1:]] <= 2025
    s_t, s_t1 = st[:-1][adj[1:]][m], st[1:][adj[1:]][m]
    perm = rng.permutation(len(s_t))
    h = len(s_t) // 2
    Ch = np.zeros((8, 8))
    np.add.at(Ch, (s_t[perm[:h]], s_t1[perm[:h]]), 1)
    Ch2 = np.zeros((8, 8))
    np.add.at(Ch2, (s_t[perm[h:]], s_t1[perm[h:]]), 1)
    Ph = Ch / np.maximum(Ch.sum(1, keepdims=True), 1)
    Ph2 = Ch2 / np.maximum(Ch2.sum(1, keepdims=True), 1)
    self_d.append(0.5 * np.abs(Ph - Ph2).sum())
self_d = np.array(self_d)
print(f'同品种半分距离: mean {self_d.mean():.4f} sd {self_d.std():.4f} max {self_d.max():.4f}')
homog = {}
for i, sa in enumerate(SYMS):
    for sb in SYMS[i + 1:]:
        dist = float(0.5 * np.abs(Pm[sa] - Pm[sb]).sum())
        z = (dist - self_d.mean()) / self_d.std()
        homog[f'{sa}-{sb}'] = {'tv': dist, 'z': z}
ds = sorted(homog.items(), key=lambda kv: -kv[1]['z'])
print('最不同 Top5:', [(k, round(v['tv'], 4), round(v['z'], 1)) for k, v in ds[:5]])
print('最相似 Top5:', [(k, round(v['tv'], 4), round(v['z'], 1)) for k, v in ds[-5:]])

print('\n=== C. 振幅相关 ===')
corr_raw, corr_dm = {}, {}
for i, sa in enumerate(SYMS):
    for sb in SYMS[i + 1:]:
        ia, ib = align(sa, sb)
        A, B = D[sa], D[sb]
        xa, xb = A['amp'][ia], B['amp'][ib]
        m = A['year'][ia] <= 2025
        xa, xb, mod = xa[m], xb[m], A['mod'][ia][m]
        # slot 去均值
        xa_dm = xa - pd.Series(xa).groupby(mod).transform('mean').values
        xb_dm = xb - pd.Series(xb).groupby(mod).transform('mean').values
        corr_raw[f'{sa}-{sb}'] = float(np.corrcoef(xa, xb)[0, 1])
        corr_dm[f'{sa}-{sb}'] = float(np.corrcoef(xa_dm, xb_dm)[0, 1])
cr = sorted(corr_raw.items(), key=lambda kv: -kv[1])
print('原始 amp 相关 Top8:', [(k, round(v, 3)) for k, v in cr[:8]])
print('原始 amp 相关 Bottom5:', [(k, round(v, 3)) for k, v in cr[-5:]])
cd = sorted(corr_dm.items(), key=lambda kv: -kv[1])
print('slot去均值相关 Top8:', [(k, round(v, 3)) for k, v in cd[:8]])
print('slot去均值相关 Bottom5:', [(k, round(v, 3)) for k, v in cd[-5:]])

import json
json.dump({'cotail': cotail, 'homog': homog, 'self_dist': {'mean': float(self_d.mean()), 'sd': float(self_d.std())},
           'corr_raw': corr_raw, 'corr_dm': corr_dm},
          open('results_sector.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved results_sector.json')
