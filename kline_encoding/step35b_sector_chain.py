# -*- coding: utf-8 -*-
"""Step 35b: 每品种一阶/二阶链 vs 边际（holdout 下一根振幅五档 log-loss）"""
import sys; sys.path.insert(0, '.')
import os
import numpy as np, pandas as pd
from ke_common import load

PARTS = ['2020', '2021_2025', '2026']
SYMS = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'UR', 'FG', 'SA', 'PF', 'PX', 'PR', 'EB', 'SC', 'IF']


def load_sk(sym):
    parts = [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
    return load(sym, parts)


res = {}
for sym in SYMS:
    d = load_sk(sym)
    st, adj, amp, yr = d.state.values, d.adj.values, d.amp.values, d.year.values
    qs = np.quantile(amp[yr <= 2025], [0.2, 0.4, 0.6, 0.8])
    cls = np.clip(np.digitize(amp, qs), 0, 4)
    pair_m = adj[1:]                       # 相邻对（同会话）
    s_t, s_t1, c_t1 = st[:-1][pair_m], st[1:][pair_m], cls[1:][pair_m]
    y = yr[1:][pair_m]
    ytr, yte = y <= 2025, y == 2026
    # 边际
    pc_marg = np.bincount(c_t1[ytr], minlength=5) / ytr.sum()
    # 一阶链 P(class_{t+1} | state_t)
    C1 = np.zeros((8, 5))
    np.add.at(C1, (s_t[ytr], c_t1[ytr]), 1)
    P1 = (C1 + 0.5) / (C1 + 0.5).sum(1, keepdims=True)
    # 二阶链 P(class_{t+2} | state_t, state_{t+1})，目标年为配对 i+1 的年
    k2 = s_t[:-1] * 8 + s_t1[:-1]
    ytr2 = ytr[1:]
    C2 = np.zeros((64, 5))
    np.add.at(C2, (k2[ytr2], c_t1[1:][ytr2]), 1)
    P2 = (C2 + 0.5) / (C2 + 0.5).sum(1, keepdims=True)
    ll_marg = float(-np.log2(np.maximum(pc_marg[c_t1[yte]], 1e-12)).mean())
    ll_m1 = float(-np.log2(np.maximum(P1[s_t[yte], c_t1[yte]], 1e-12)).mean())
    yte2 = yte[1:]
    ll_m2 = float(-np.log2(np.maximum(P2[k2[yte2], c_t1[1:][yte2]], 1e-12)).mean())
    res[sym] = {'marg': ll_marg, 'm1': ll_m1, 'm2': ll_m2, 'n_holdout': int(yte.sum())}
    print(f'{sym}: marg {ll_marg:.3f} | 一阶 {ll_m1:.3f} | 二阶 {ll_m2:.3f} (n={yte.sum():,})')

import json
json.dump(res, open('results_sector_chain.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved')
