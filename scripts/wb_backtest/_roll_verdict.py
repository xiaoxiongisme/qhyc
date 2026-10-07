# -*- coding: utf-8 -*-
"""滚动品种池筛选判决：网格高原 / 半样本 / 去 TOP 品种 / 风险调整 / 聚类 bootstrap。"""
import os
import json
import numpy as np
import pandas as pd

OUT = '/tmp/roll_out'
GR = json.load(open(os.path.join(OUT, 'grid_results.json'), encoding='utf-8'))
RES = GR['results']
NAMES = [n for n in RES if n != 'BASE']
BASE = RES['BASE']
bdf = pd.read_csv(os.path.join(OUT, 'g_trades_BASE.csv'), encoding='utf-8-sig')

print('=' * 104)
print('OOS 2021-01-04 → 2026-09-30 | max10 / hold30 / 4×ATR60 | 基线(不筛) n=%d net=%.0f'
      % (BASE['n'], BASE['net']))
print('=' * 104)


def df_of(n):
    return pd.read_csv(os.path.join(OUT, 'g_trades_%s.csv' % n),
                       encoding='utf-8-sig')


D = {n: df_of(n) for n in NAMES}

# ---------- 1. 网格总表（按净降序） ----------
print('\n[1] 参数网格 27 配置（按净降序）   Δ=与基线之差')
print('%-18s %5s %10s %10s %6s %8s %10s %10s %8s %8s'
      % ('配置', '笔数', '净', 'Δ', 'PF', '单笔', '峰值保证金', 'MDD', '净/|MDD|', '净/峰值保'))
rows = sorted(NAMES, key=lambda n: -RES[n]['net'])
for n in rows:
    m = RES[n]
    print('%-18s %5d %10.0f %+10.0f %6.3f %8.0f %10.0f %10.0f %8.2f %8.2f'
          % (n, m['n'], m['net'], m['net'] - BASE['net'], m['pf'], m['per_trade'],
             m['peak_margin'], m['mdd'],
             m['net'] / abs(m['mdd']) if m['mdd'] else 0,
             m['net'] / m['peak_margin'] if m['peak_margin'] else 0))
print('%-18s %5d %10.0f %10s %6.3f %8.0f %10.0f %10.0f %8.2f %8.2f'
      % ('BASE(不筛)', BASE['n'], BASE['net'], '-', BASE['pf'], BASE['per_trade'],
         BASE['peak_margin'], BASE['mdd'],
         BASE['net'] / abs(BASE['mdd']), BASE['net'] / BASE['peak_margin']))

pos = sum(1 for n in NAMES if RES[n]['net'] > BASE['net'])
better_pf = sum(1 for n in NAMES if RES[n]['pf'] > BASE['pf'])
better_rr = sum(1 for n in NAMES
                if RES[n]['net'] / abs(RES[n]['mdd']) > BASE['net'] / abs(BASE['mdd']))
print('\n  优于基线的格子：净 %d/%d   PF %d/%d   净/|MDD| %d/%d'
      % (pos, len(NAMES), better_pf, len(NAMES), better_rr, len(NAMES)))

# ---------- 2. 高原检验：沿每个维度看相邻档位移 ----------
print('\n[2] 高原检验（沿单维度固定其余，看净的抖动幅度）')
for dim, other in (('rebal', None), ('win', None), ('rule', None)):
    pass
grid = {}
for n in NAMES:
    _, a, b, c = n.split('_')
    grid[(a, b, c)] = RES[n]['net']
for fixed_w, fixed_r in (('W3', 'POS'), ('W3', 'T15'), ('W5', 'POS')):
    v = [grid[(a, fixed_w, fixed_r)] for a in ('1Y', '6M', '3M')]
    print('  回看%s/规则%s：调仓 1Y/6M/3M → %.0f / %.0f / %.0f  (极差 %.0f, 相邻均位移 %.0f)'
          % (fixed_w, fixed_r, v[0], v[1], v[2], max(v) - min(v),
             (abs(v[1] - v[0]) + abs(v[2] - v[1])) / 2))
for fixed_a, fixed_r in (('1Y', 'POS'), ('6M', 'POS')):
    v = [grid[(fixed_a, w, fixed_r)] for w in ('W2', 'W3', 'W5')]
    print('  调仓%s/规则%s：回看 2y/3y/5y → %.0f / %.0f / %.0f  (极差 %.0f, 相邻均位移 %.0f)'
          % (fixed_a, fixed_r, v[0], v[1], v[2], max(v) - min(v),
             (abs(v[1] - v[0]) + abs(v[2] - v[1])) / 2))
for fixed_a, fixed_w in (('1Y', 'W3'), ('6M', 'W3')):
    v = [grid[(fixed_a, fixed_w, r)] for r in ('POS', 'T15', 'T20')]
    print('  调仓%s/回看%s：规则 POS/T15/T20 → %.0f / %.0f / %.0f  (极差 %.0f, 相邻均位移 %.0f)'
          % (fixed_a, fixed_w, v[0], v[1], v[2], max(v) - min(v),
             (abs(v[1] - v[0]) + abs(v[2] - v[1])) / 2))

# ---------- 3. 半样本（OOS 内部再对切） ----------
print('\n[3] OOS 内部半样本对切（Δ = 变体 − 基线，元）')
h1 = (2021, 2023)
h2 = (2024, 2026)
b1 = float(bdf[(bdf.eyear >= h1[0]) & (bdf.eyear <= h1[1])].pnl.sum())
b2 = float(bdf[(bdf.eyear >= h2[0]) & (bdf.eyear <= h2[1])].pnl.sum())
print('%-18s %12s %12s %12s %12s' % ('配置', '前半Δ', '后半Δ', '同号?', '最小半Δ'))
both = 0
for n in rows:
    d = D[n]
    a1 = float(d[(d.eyear >= h1[0]) & (d.eyear <= h1[1])].pnl.sum()) - b1
    a2 = float(d[(d.eyear >= h2[0]) & (d.eyear <= h2[1])].pnl.sum()) - b2
    same = (a1 > 0) == (a2 > 0)
    both += 1 if (a1 > 0 and a2 > 0) else 0
    print('%-18s %+12.0f %+12.0f %12s %+12.0f'
          % (n, a1, a2, '是' if same else '否', min(a1, a2)))
print('  两个半样本同时为正的配置：%d/%d' % (both, len(rows)))

# ---------- 4. 去 TOP 品种 ----------
print('\n[4] 剔除贡献最大品种后（元）  基线 TOP1 = %s'
      % BASE['top5'][0])
print('%-18s %11s %11s %11s %11s' % ('配置', '原始Δ', '剔top1后Δ', '剔top3后Δ', '剔top5后Δ'))
base_rank = bdf.groupby('symbol').pnl.sum().sort_values(ascending=False)
keep1, keep3 = [], []
for n in rows:
    d = D[n]
    r = d.groupby('symbol').pnl.sum().sort_values(ascending=False)
    vals = []
    for k in (1, 3, 5):
        drop = set(base_rank.head(k).index)      # 用「基线口径」的 top 品种，避免自证
        vals.append(float(d[~d.symbol.isin(drop)].pnl.sum())
                    - float(bdf[~bdf.symbol.isin(drop)].pnl.sum()))
    if vals[0] > 0:
        keep1.append(n)
    if vals[2] > 0:
        keep3.append(n)
    print('%-18s %+11.0f %+11.0f %+11.0f %+11.0f'
          % (n, RES[n]['net'] - BASE['net'], vals[0], vals[1], vals[2]))
print('  剔 top1 后仍优于基线：%d/%d ；剔 top5 后仍优于基线：%d/%d'
      % (len(keep1), len(rows), len(keep3), len(rows)))

# ---------- 5. 品种聚类 bootstrap ----------
print('\n[5] 品种聚类 bootstrap 2000 次（Δ = 变体 − 基线，按品种重抽样）')
rng = np.random.default_rng(20261007)
syms = np.array(sorted(set(bdf.symbol) | set().union(*[set(D[n].symbol) for n in rows])))
gbase = bdf.groupby('symbol').pnl.sum().reindex(syms).fillna(0.0).to_numpy()
NS = len(syms)
B = 2000
print('%-18s %10s %10s %10s %8s' % ('配置', '点估计', '中位', '5%分位', 'P(Δ>0)'))
sig = []
for n in rows:
    gv = D[n].groupby('symbol').pnl.sum().reindex(syms).fillna(0.0).to_numpy()
    d = gv - gbase
    s = d[rng.integers(0, NS, size=(B, NS))].sum(axis=1)
    p = 100 * float((s > 0).mean())
    if p >= 95:
        sig.append(n)
    print('%-18s %+10.0f %+10.0f %+10.0f %7.1f%%'
          % (n, d.sum(), np.median(s), np.percentile(s, 5), p))
print('  P(Δ>0) ≥ 95%% 的配置：%s' % (sig if sig else '无'))

# ---------- 6. 剔 top1 后再做 bootstrap ----------
print('\n[6] 剔除基线 top1 品种后重做 bootstrap（检验增益是否只来自单一品种）')
syms2 = np.array([s for s in syms if s != base_rank.index[0]])
gbase2 = bdf[~bdf.symbol.isin({base_rank.index[0]})].groupby('symbol').pnl.sum()
gbase2 = gbase2.reindex(syms2).fillna(0.0).to_numpy()
NS2 = len(syms2)
print('%-18s %10s %10s %8s' % ('配置', '点估计', '中位', 'P(Δ>0)'))
for n in rows:
    d2 = D[n][~D[n].symbol.isin({base_rank.index[0]})]
    gv2 = d2.groupby('symbol').pnl.sum().reindex(syms2).fillna(0.0).to_numpy()
    d = gv2 - gbase2
    s = d[rng.integers(0, NS2, size=(B, NS2))].sum(axis=1)
    print('%-18s %+10.0f %+10.0f %7.1f%%'
          % (n, d.sum(), np.median(s), 100 * float((s > 0).mean())))
print('\nDONE')
