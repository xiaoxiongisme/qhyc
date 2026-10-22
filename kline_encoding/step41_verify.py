# -*- coding: utf-8 -*-
"""step41: 结论链整体复验（独立重算，不复用旧分析代码）
V1 开盘振幅集中（U 型）         V2 前波幅→下根振幅 IC      V3 clv 均值回归（含时段定位）
V4 2026-04-08 挤仓事实         V5 15 品种池化毛边缘 h=1    V6 波动体制持续性（lift）
V7 MA-EG 共尾：朴素 vs slot 调整
"""
import os, json
import numpy as np
import pandas as pd
from ke_common import load, spearman

PARTS = ['2020', '2021_2025', '2026']
SYMS15 = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'UR', 'FG', 'SA', 'PF', 'PX', 'PR', 'EB', 'SC', 'IF']

def load_sk(sym):
    return load(sym, [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300])

V = {}

# ---------- V1 开盘集中 ----------
d = load_sk('MA')
mtr = d.year.values <= 2025
amp = d.amp.values
slot = d.slot.values
seg = d.seg.values
seq = d.seq.values
sess = pd.Series(amp).groupby(seg)
first15 = pd.Series(np.where(seq < 15, amp, 0.0)).groupby(seg).sum()
total = sess.sum()
share_s = (first15 / total.replace(0, np.nan)).dropna()
seg_yr_s = pd.Series(d.year.values).groupby(seg).first()
V['v1_open_share_all'] = float(share_s.mean())                      # 全期
V['v1_open_share_train_only'] = float(share_s[seg_yr_s[share_s.index] <= 2025].mean())
V['v1_amp_open_bp'] = float(np.mean(amp[mtr & (seq < 15)]) * 100)
V['v1_amp_rest_bp'] = float(np.mean(amp[mtr & (seq >= 15)]) * 100)

# ---------- V2 前波幅→下根振幅 IC ----------
res2 = {}
for sym in ['MA', 'EG']:
    dd = load_sk(sym)
    a, adjv, yr = dd.amp.values, dd.adj.values, dd.year.values
    ap = np.concatenate([[a[0]], a[:-1]])
    m = adjv & (dd.seq.values > 0)
    res2[sym] = {'train': float(spearman(ap[m & (yr <= 2025)], a[m & (yr <= 2025)])),
                 'hold': float(spearman(ap[m & (yr == 2026)], a[m & (yr == 2026)]))}
V['v2_volamp_ic'] = res2

# ---------- V3 clv MR（含时段定位） ----------
res3 = {}
for sym in ['MA', 'EG']:
    dd = load_sk(sym)
    cv, lrv, adjv, yr, sl = dd.clv.values, dd.logret.values, dd.adj.values, dd.year.values, dd.slot.values
    cvp = np.concatenate([[cv[0]], cv[:-1]])
    m = adjv & (dd.seq.values > 0)
    tr = m & (yr <= 2025)
    res3[sym] = {'train': float(spearman(cvp[tr], lrv[tr])),
                 'hold': float(spearman(cvp[m & (yr == 2026)], lrv[m & (yr == 2026)])),
                 'open9': float(spearman(cvp[tr & (sl < 15)], lrv[tr & (sl < 15)])),
                 'night_open': float(spearman(cvp[tr & (sl >= 225) & (sl < 240)], lrv[tr & (sl >= 225) & (sl < 240)])),
                 'night_close': float(spearman(cvp[tr & (sl >= 330)], lrv[tr & (sl >= 330)])),
                 'midday': float(spearman(cvp[tr & (sl >= 135) & (sl < 210)], lrv[tr & (sl >= 135) & (sl < 210)]))}
V['v3_clv_mr'] = res3

# ---------- V4 2026-04-08 挤仓事实（MA） ----------
d26 = d[d.year.values == 2026].reset_index(drop=True)
gap = d26.datetime.diff().dt.total_seconds().values
new_sess = np.concatenate([[True], gap[1:] != 60.0])
pc = d26.close.values
op = d26.open.values
idx_open = np.where(new_sess)[0]
idx_open = idx_open[idx_open > 0]
gaps = (op[idx_open] - pc[idx_open - 1]) / pc[idx_open - 1] * 1e4
worst = int(np.argmin(gaps))
oi, od = idx_open[worst], d26.datetime.values[idx_open[worst]]
sess_mask = d26.seg.values == d26.seg.values[idx_open[worst]]
first_bar_amp = float(d26.amp.values[idx_open[worst]])
vol_first = float(d26.volume.values[idx_open[worst]])
vol_med = float(np.median(d26.volume.values[(d26.amp.values > 0)]))
c26 = d26.close.values
i_peak = int(np.argmax(c26))
peak_date = d26.datetime.values[i_peak]
dd_end = (c26[-1] / c26[i_peak] - 1) * 1e4
V['v4_squeeze26'] = {'worst_gap_bp': float(gaps[worst]), 'worst_gap_bar': str(od)[:16],
                     'first_bar_amp_pct': first_bar_amp,
                     'first_bar_vol_vs_median': vol_first / vol_med,
                     'peak_date': str(peak_date)[:10], 'peak_to_end_bp': float(dd_end)}

# ---------- V5 池化毛边缘（15 品种 h=1，独立重写） ----------
HI, LO = 0.8, 0.2
all_g = []
per = {}
for sym in SYMS15:
    dd = load_sk(sym)
    cv, lrv, adjv, yr, segv = dd.clv.values, dd.logret.values, dd.adj.values, dd.year.values, dd.seg.values
    n = len(dd)
    fwd = np.full(n, np.nan)
    t = np.arange(n)
    t2 = t + 1
    m2 = (t2 < n) & (segv[np.minimum(t2, n - 1)] == segv)
    fwd[m2] = lrv[np.minimum(t2, n - 1)][m2]
    sig = np.where(cv >= HI, -1.0, np.where(cv <= LO, 1.0, 0.0))
    m = (sig != 0) & adjv & (yr <= 2025) & np.isfinite(fwd)
    g = sig[m] * fwd[m] * 1e4
    per[sym] = {'n': int(len(g)), 'gross_bp': float(np.mean(g)) if len(g) else None}
    all_g.append(g)
G = np.concatenate(all_g)
V['v5_pooled'] = {'n': int(len(G)), 'gross_bp': float(np.mean(G)),
                  't': float(np.mean(G) / (np.std(G) / np.sqrt(len(G)))),
                  'per_sym': per}

# ---------- V6 体制持续性 lift ----------
res6 = {}
for sym in ['MA', 'EG']:
    dd = load_sk(sym)
    a, adjv, yr = dd.amp.values, dd.adj.values, dd.year.values
    q80 = np.quantile(a[yr <= 2025], 0.8)
    hot = (a > q80).astype(int)
    hp = np.concatenate([[hot[0]], hot[:-1]])
    m = adjv & (dd.seq.values > 0) & (yr <= 2025)
    p_joint = np.mean((hp[m] == 1) & (hot[m] == 1))
    p_hot = np.mean(hot[m] == 1)
    res6[sym] = {'lift': float(p_joint / p_hot ** 2), 'p_next_given_hot': float(p_joint / p_hot)}
V['v6_regime_lift'] = res6

# ---------- V7 MA-EG 共尾（朴素 vs slot 调整，train） ----------
e = load_sk('EG')
j = d[['datetime', 'amp', 'year', 'slot']].merge(
    e[['datetime', 'amp']], on='datetime', suffixes=('_ma', '_eg'))
jtr = j[j.year <= 2025]
q_ma, q_eg = np.quantile(jtr.amp_ma, 0.99), np.quantile(jtr.amp_eg, 0.99)
hot_ma, hot_eg = (jtr.amp_ma > q_ma).values, (jtr.amp_eg > q_eg).values
naive = hot_ma.mean() * hot_eg.mean()
obs = np.mean(hot_ma & hot_eg)
V['v7_cotail'] = {'naive_mult': float(obs / naive)}
# slot 调整（分钟桶 + EB α=10，独立重写）
sl = jtr.slot.values.astype(int)
cnt_hot = np.zeros(345); cnt_all = np.zeros(345)
np.add.at(cnt_all, sl, 1.0)
np.add.at(cnt_hot, sl, hot_ma)
p_ma_slot = (cnt_hot + 10 * 0.01) / (cnt_all + 10)
cnt_hot2 = np.zeros(345); np.add.at(cnt_hot2, sl, hot_eg)
p_eg_slot = (cnt_hot2 + 10 * 0.01) / (cnt_all + 10)
exp_slot = float(np.mean(p_ma_slot[sl] * p_eg_slot[sl]))   # 每根 bar 按自身 slot 的条件独立期望
V['v7_cotail']['slot_adj_mult'] = float(obs / exp_slot)

print(json.dumps(V, ensure_ascii=False, indent=1))
json.dump(V, open('results_verify.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
