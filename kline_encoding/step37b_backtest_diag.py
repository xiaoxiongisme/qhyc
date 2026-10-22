# -*- coding: utf-8 -*-
"""Step 37b: 回测诊断——毛边缘分解、阈值敏感性、时段集中度、盈亏平衡成本、容量线性"""
import sys; sys.path.insert(0, '.')
import os
import json
import numpy as np
import pandas as pd
from ke_common import load

PARTS = ['2020', '2021_2025', '2026']
SYMS = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'UR', 'FG', 'SA', 'PF', 'PX', 'PR', 'EB', 'SC', 'IF']
MULT = {'MA': 10, 'EG': 10, 'TA': 5, 'PP': 5, 'L': 5, 'V': 5, 'UR': 20, 'FG': 20, 'SA': 20,
        'PF': 5, 'PX': 5, 'PR': 5, 'EB': 5, 'SC': 1000, 'IF': 300}


def load_sk(sym):
    parts = [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
    return load(sym, parts)


def fwd_ret(d, h):
    lr = d.logret.values
    cs = pd.Series(lr).groupby(d.seg.values).cumsum().values
    segv = d.seg.values
    t = np.arange(len(d))
    t2 = t + h
    f = np.full(len(d), np.nan)
    m = (t2 < len(d)) & (segv[np.minimum(t2, len(d) - 1)] == segv)
    f[m] = cs[t2[m]] - cs[t[m]]
    return f


out = {}
print('品种 | h | 毛边缘bp | 成本12bp后 | 阈值0.8毛 | 阈值0.95毛 | 开盘段毛 | 其余段毛')
for sym in SYMS:
    d = load_sk(sym)
    yr = d.year.values
    mtr = yr <= 2025
    clv = d.clv.values
    adj = d.adj.values
    slot = d.slot.values
    r = {}
    for h in (1, 5, 15, 60):
        f = fwd_ret(d, h)
        row = {}
        for q, lbl in ((0.8, 'q80'), (0.95, 'q95')):
            HI = np.quantile(clv[mtr], q); LO = np.quantile(clv[mtr], 1 - q)
            sig = np.zeros(len(d))
            sig[(clv >= HI) & adj] = -1.0
            sig[(clv <= LO) & adj] = 1.0
            idx = np.where((sig != 0) & np.isfinite(f))[0]
            g = sig[idx] * f[idx]
            row[f'gross_{lbl}_bp'] = float(g.mean() * 1e4)
            row[f'n_{lbl}'] = int(len(idx))
        # 毛边缘的时段集中（q80 阈值）
        HI = np.quantile(clv[mtr], 0.8); LO = np.quantile(clv[mtr], 0.2)
        sig = np.zeros(len(d))
        sig[(clv >= HI) & adj] = -1.0
        sig[(clv <= LO) & adj] = 1.0
        idx = np.where((sig != 0) & np.isfinite(f))[0]
        g = sig[idx] * f[idx]
        open_m = (slot[idx] < 15) | ((slot[idx] >= 75) & (slot[idx] < 90)) | \
                 ((slot[idx] >= 135) & (slot[idx] < 150)) | (slot[idx] >= 339)
        row['gross_open_bp'] = float(g[open_m].mean() * 1e4) if open_m.sum() > 30 else None
        row['gross_nonopen_bp'] = float(g[~open_m].mean() * 1e4) if (~open_m).sum() > 30 else None
        row['n'] = int(len(idx))
        # 盈亏平衡成本
        row['breakeven_cost_bp'] = row['gross_q80_bp']
        r[f'h{h}'] = row
    out[sym] = r
    h1 = r['h1']
    print(f"{sym}: h1 毛 {h1['gross_q80_bp']:+.2f}bp (n={h1['n']:,}) | q95 {h1['gross_q95_bp']:+.2f}bp "
          f"(n={h1['n_q95']:,}) | 开盘 {h1['gross_open_bp']:+.2f} / 其余 {h1['gross_nonopen_bp']:+.2f}bp "
          f"| h5 毛 {r['h5']['gross_q80_bp']:+.2f} h15 {r['h15']['gross_q80_bp']:+.2f} h60 {r['h60']['gross_q80_bp']:+.2f}")

# 汇总：全品种池化（h=1, q80，train）
print('\n=== 池化与容量 ===')
all_g = []
for sym in SYMS:
    d = load_sk(sym)
    yr = d.year.values; mtr = yr <= 2025
    clv = d.clv.values; adj = d.adj.values
    f = fwd_ret(d, 1)
    HI = np.quantile(clv[mtr], 0.8); LO = np.quantile(clv[mtr], 0.2)
    sig = np.zeros(len(d))
    sig[(clv >= HI) & adj] = -1.0
    sig[(clv <= LO) & adj] = 1.0
    idx = np.where((sig != 0) & np.isfinite(f))[0]
    all_g.append(sig[idx] * f[idx])
all_g = np.concatenate(all_g)
print(f'池化毛边缘 h1: {all_g.mean()*1e4:+.2f}bp (n={len(all_g):,}), t={all_g.mean()/all_g.std()*np.sqrt(len(all_g)):.1f}')
print(f'盈亏平衡往返成本 = {all_g.mean()*1e4:.2f}bp = 每边 {all_g.mean()*1e4/2:.2f}bp')
json.dump(out, open('results_backtest_diag.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=float)
print('saved')
