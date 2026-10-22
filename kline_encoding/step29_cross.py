# -*- coding: utf-8 -*-
"""S3 跨品种共尾深挖（MA × EG）。
(a) 条件共尾：分年（含 2026 holdout）、日盘/夜盘、周一/其他、高波环境、多分位
(b) 精细领先滞后 k=±1..15 分钟 + 方向不对称（涨极值/跌极值 2x2）
(c) 60 交易日滚动共尾倍数稳定性
(d) 组合风险加总：联合 99% 分位 vs 单品种分位加总
口径：同一分钟对齐（inner merge）；极端 = 振幅超 train 分位；train=2020-2025 定阈值，holdout=2026 验证。
"""
import numpy as np, pandas as pd
from ke_common import load, save_json

PARTS = ['2020', '2021_2025', '2026']
d1 = load('MA', PARTS)
d2 = load('EG', PARTS)
cols = ['datetime', 'amp', 'chg', 'state', 'slot', 'td', 'dow', 'year', 'seg']
m = pd.merge(d1[cols], d2[cols], on='datetime', suffixes=('_a', '_b'), how='inner')
n = len(m)
cov = {'n_ma': len(d1), 'n_eg': len(d2), 'n_merged': int(n),
       'match_rate': float(n / max(len(d1), len(d2))),
       'by_year': {int(y): int((m.year_a == y).sum()) for y in range(2020, 2027)}}
print('合并覆盖:', cov)

amp_a, amp_b = m.amp_a.values, m.amp_b.values
chg_a, chg_b = m.chg_a.values, m.chg_b.values
m_tr = m.year_a.values < 2026
m_te = ~m_tr
slot = m.slot_a.values
dow = m.dow_a.values
td = m.td_a.values

# 高波环境：两品种各自过去 60 根已实现振幅 > train 中位
def roll_med_mask(amp, tr_mask, win=60):
    s = pd.Series(amp)
    med = np.median(amp[tr_mask])
    return (s.rolling(win, min_periods=win // 2).mean().values > med), med

hv_a, med_a = roll_med_mask(amp_a, m_tr)
hv_b, med_b = roll_med_mask(amp_b, m_tr)
hv = hv_a & hv_b


def cotail(q, mask_tr, mask_ev=None):
    """同一分钟联合超越倍数 = P(A>q且B>q) / (P(A>q)P(B>q))；mask_ev 为评估子集（默认 train）"""
    ta = np.quantile(amp_a[mask_tr], q); tb = np.quantile(amp_b[mask_tr], q)
    ea = amp_a > ta; eb = amp_b > tb
    if mask_ev is None: mask_ev = mask_tr
    N = mask_ev.sum()
    pa = ea[mask_ev].mean(); pb = eb[mask_ev].mean()
    pj = (ea[mask_ev] & eb[mask_ev]).mean()
    return {'q': q, 'n': int(N), 'pa': float(pa), 'pb': float(pb),
            'p_joint': float(pj), 'expected': float(pa * pb),
            'multiplier': float(pj / max(pa * pb, 1e-12))}


# ---------- (a) 条件共尾 ----------
cond = {}
for q in [0.95, 0.975, 0.99, 0.995]:
    cond[str(q)] = {'train': cotail(q, m_tr), 'holdout': cotail(q, m_tr, m_te)}
conds = {
    'day': m_tr & (slot < 225), 'night': m_tr & (slot >= 225),
    'mon': m_tr & (dow == 0), 'tuefri': m_tr & (dow > 0) & (dow < 5),
    'highvol': m_tr & hv, 'normal': m_tr & ~hv,
}
cond_detail = {}
for nm, mk in conds.items():
    cond_detail[nm] = {str(q): cotail(q, m_tr, mk) for q in [0.95, 0.99, 0.995]}
by_year = {}
for y in range(2020, 2027):
    my = m.year_a.values == y
    by_year[str(y)] = {str(q): cotail(q, m_tr, my) for q in [0.95, 0.99, 0.995]}

# ---------- (b) 精细领先滞后 ----------
def leadlag(kmax=15, q=0.99):
    ta = np.quantile(amp_a[m_tr], q); tb = np.quantile(amp_b[m_tr], q)
    ea = amp_a > ta; eb = amp_b > tb
    out = {}
    for k in range(-kmax, kmax + 1):
        if k == 0: continue
        if k > 0:
            # A 在 t-k 极端 → B 在 t 极端（A 领先）
            pa = ea[:n - k]; pb = eb[k:]
            base = pa.mean() * pb.mean()
            out[str(k)] = float((pa & pb).mean() / max(base, 1e-12))
        else:
            pa = eb[:n + k]; pb = ea[-k:]   # B 领先 |k| 分钟
            base = pa.mean() * pb.mean()
            out[str(k)] = float((pa & pb).mean() / max(base, 1e-12))
    return out

ll = leadlag()
# 方向不对称：k=0 与 |k| 峰值处，涨/跌极值 2x2
ta99 = np.quantile(amp_a[m_tr], 0.99); tb99 = np.quantile(amp_b[m_tr], 0.99)
ea = amp_a > ta99; eb = amp_b > tb99
ua = chg_a > 0; ub = chg_b > 0
def dir_mult(ma_, mb_, mask=None):
    mask = np.ones(n, bool) if mask is None else mask
    p = (ma_[mask] & mb_[mask]).mean()
    base = ma_[mask].mean() * mb_[mask].mean()
    return float(p / max(base, 1e-12)), int((ma_[mask] & mb_[mask]).sum())
dirs = {}
for nm, (x, y) in {'A涨→B涨': (ea & ua, eb & ub), 'A涨→B跌': (ea & ua, eb & ~ub),
                   'A跌→B涨': (ea & ~ua, eb & ub), 'A跌→B跌': (ea & ~ua, eb & ~ub)}.items():
    dirs[nm] = {'train': dir_mult(x, y, m_tr), 'holdout': dir_mult(x, y, m_te)}

# ---------- (c) 滚动共尾 ----------
ta = np.quantile(amp_a[m_tr], 0.99); tb = np.quantile(amp_b[m_tr], 0.99)
ea = amp_a > ta; eb = amp_b > tb
df = pd.DataFrame({'td': td, 'ea': ea, 'eb': eb, 'yr': m.year_a.values})
g = df.groupby('td')
day = pd.DataFrame({'n': g.size(), 'ja': g.ea.sum(), 'jb': g.eb.sum(), 'jab': g.apply(lambda x: (x.ea & x.eb).sum())})
day['mult'] = (day.jab / day.n) / np.maximum((day.ja / day.n) * (day.jb / day.n), 1e-12)
day = day[day.n >= 100]
roll = day['mult'].rolling(60, min_periods=30)
roll_stats = {'mean': float(roll.mean().mean()), 'sd': float(roll.std().mean()),
              'min': float(roll.mean().min()), 'max': float(roll.mean().max()),
              'n_days': int(len(day))}
roll_series = [(str(i), round(float(v), 2)) for i, v in roll.mean().dropna().items()]

# ---------- (d) 组合加总 ----------
r1 = chg_a / 100.0; r2 = chg_b / 100.0
loss_q = 0.01
qa = np.quantile(r1[m_tr], loss_q); qb = np.quantile(r2[m_tr], loss_q)
qsum_tr = np.quantile((r1 + r2)[m_tr], loss_q)
qsum_te = np.quantile((r1 + r2)[m_te], loss_q)
# 独立性基准：Var(r1+r2)=Var(r1)+Var(r2) → 1% 分位约按 sqrt(2) 缩放
indep_ratio = float(np.sqrt(2))
port = {'q99_a': float(qa), 'q99_b': float(qb), 'q99_sum_marginals': float(qa + qb),
        'q99_joint_train': float(qsum_tr), 'q99_joint_holdout': float(qsum_te),
        'excess_ratio_train': float(qsum_tr / (qa + qb)),
        'excess_ratio_holdout': float(qsum_te / (qa + qb)),
        'indep_benchmark': indep_ratio}

# ---------- (e) slot 调整基准：剔除共享日内季节性后的共尾 ----------
def slot_adj(q=0.99, mask_ev=None):
    ta_ = np.quantile(amp_a[m_tr], q); tb_ = np.quantile(amp_b[m_tr], q)
    ea_ = amp_a > ta_; eb_ = amp_b > tb_
    if mask_ev is None: mask_ev = m_tr
    pa_s = np.zeros(345); pb_s = np.zeros(345); ns = np.zeros(345)
    for s in range(345):
        msk = mask_ev & (slot == s)
        ns[s] = msk.sum()
        if ns[s] > 0:
            pa_s[s] = ea_[msk].mean(); pb_s[s] = eb_[msk].mean()
    expected = float((ns * pa_s * pb_s).sum() / max(ns.sum(), 1))
    observed = float((ea_[mask_ev] & eb_[mask_ev]).mean())
    return {'observed': observed, 'expected_slot_indep': expected,
            'multiplier': observed / max(expected, 1e-15),
            'raw_multiplier': observed / max(ea_[mask_ev].mean() * eb_[mask_ev].mean(), 1e-15)}

slot_adj_res = {'train': slot_adj(0.99), 'holdout': slot_adj(0.99, m_te)}
for q in [0.95, 0.99, 0.995]:
    slot_adj_res['train_q%s' % q] = slot_adj(q)
slot_adj_by_year = {}
for y in range(2020, 2027):
    my = m.year_a.values == y
    slot_adj_by_year[str(y)] = slot_adj(0.99, my)
# slot 调整后的方向 2x2（train）
def dir_mult_adj(x, y, q=0.99):
    ta_ = np.quantile(amp_a[m_tr], q); tb_ = np.quantile(amp_b[m_tr], q)
    ea_ = amp_a > ta_; eb_ = amp_b > tb_
    xs = x & ea_; ys = y & eb_
    ex = 0.0
    for s in range(345):
        msk = m_tr & (slot == s)
        if msk.sum() > 0: ex += msk.sum() * xs[msk].mean() * ys[msk].mean()
    ex /= m_tr.sum()
    ob = (xs[m_tr] & ys[m_tr]).mean()
    return float(ob / max(ex, 1e-15)), int((xs[m_tr] & ys[m_tr]).sum())
dirs_adj = {}
for nm, (x, y) in {'A涨→B涨': (ua, ub), 'A涨→B跌': (ua, ~ub),
                   'A跌→B涨': (~ua, ub), 'A跌→B跌': (~ua, ~ub)}.items():
    dirs_adj[nm] = dir_mult_adj(x, y)
# 经验独立性基准：把 EG 序列整体错位 20 万根（破坏跨品种对齐、保留各自时间结构）
shift = 200000
r2s = np.roll(r2, shift)
qsum_null = np.quantile((r1 + r2s)[m_tr], loss_q)
port['null_shuffle_ratio'] = float(qsum_null / (qa + qb))
qa_te = np.quantile(r1[m_te], loss_q); qb_te = np.quantile(r2[m_te], loss_q)
port['q99_joint_holdout_vs_holdout_marg'] = float(qsum_te / (qa_te + qb_te))
port['qa_te'] = float(qa_te); port['qb_te'] = float(qb_te)
# amp 相关性（解释逐年倍数摆动）
amp_corr = {str(y): float(np.corrcoef(amp_a[m.year_a.values == y], amp_b[m.year_a.values == y])[0, 1]) for y in range(2020, 2027)}

RES = {'coverage': cov, 'cotail_by_q': cond, 'cond_detail': cond_detail, 'by_year': by_year,
       'leadlag': ll, 'dirs': dirs, 'dirs_adj': dirs_adj, 'roll': roll_stats, 'roll_series': roll_series,
       'port': port, 'slot_adj': slot_adj_res, 'slot_adj_by_year': slot_adj_by_year, 'amp_corr': amp_corr}
save_json(RES, 'results_cross.json')
print('共尾倍数(train):', {k: round(v['train']['multiplier'], 1) for k, v in cond.items()})
print('共尾倍数(holdout):', {k: round(v['holdout']['multiplier'], 1) for k, v in cond.items()})
print('条件共尾(q=0.99):', {k: round(v['0.99']['multiplier'], 1) for k, v in cond_detail.items()})
print('分年(q=0.99):', {k: round(v['0.99']['multiplier'], 1) for k, v in by_year.items()})
print('领先滞后峰值:', sorted(ll.items(), key=lambda x: -x[1])[:3], '反序:', sorted(ll.items(), key=lambda x: x[1])[:3])
print('方向 2x2:', {k: (round(v['train'][0], 1), v['train'][1]) for k, v in dirs.items()})
print('滚动: mean=%.1f sd=%.1f min=%.1f max=%.1f' % (roll_stats['mean'], roll_stats['sd'], roll_stats['min'], roll_stats['max']))
print('组合: q99联合/边际加总 train %.3f holdout %.3f (独立性基准 %.3f)' % (
    port['excess_ratio_train'], port['excess_ratio_holdout'], port['indep_benchmark']))
print('slot调整共尾(train): raw %.1f× → slot调整 %.1f× | holdout: raw %.1f× → slot调整 %.1f×' % (
    slot_adj_res['train']['raw_multiplier'], slot_adj_res['train']['multiplier'],
    slot_adj_res['holdout']['raw_multiplier'], slot_adj_res['holdout']['multiplier']))
print('slot调整分年(q=.99):', {k: round(v['multiplier'], 1) for k, v in slot_adj_by_year.items()})
print('slot调整方向2x2:', {k: (round(v[0], 1), v[1]) for k, v in dirs_adj.items()})
print('组合: train %.3f holdout(vs holdout边际) %.3f | 错位零基准 %.3f' % (
    port['excess_ratio_train'], port['q99_joint_holdout_vs_holdout_marg'], port['null_shuffle_ratio']))
print('amp相关性分年:', {k: round(v, 3) for k, v in amp_corr.items()})
