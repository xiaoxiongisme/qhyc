# -*- coding: utf-8 -*-
"""Step 32: 盲区扫描
A. 覆盖度审计（重复 bar / 交易日 bar 数 / 短会话 / OHLC 一致性 / 各年区间）
B. 换月与跳变审计（会话内 open≠前收 / 会话间跳空 / OI 断档 / 极端 bar 与 top 振幅的污染占比）
C. 编码有效性（三段熵 / 8 态频次与熵 / 年度漂移 / clv 格点 / 零振幅零成交）
D. 信号周期扩展（clv→未来 h 根收益 IC；logvol→未来 h 根振幅 IC；夜盘→当日、日级自相关）
"""
import sys; sys.path.insert(0, '.')
import numpy as np, pandas as pd
from scipy.stats import spearmanr
from ke_common import load, STATES, TRIG

PARTS = ['2020', '2021_2025', '2026']
SEGLEN = {9: 75, 10: 60, 13: 90, 21: 120}
EXPECT_TD = {0: 225, 1: 345, 2: 345, 3: 345, 4: 345, 5: 120}   # 周一~周六（周日 0）


def H(p):
    p = np.asarray(p, float)
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


def rank_ic(a, b):
    a = np.asarray(a, float); b = np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 50:
        return None
    return float(spearmanr(a[m], b[m]).statistic)


out = {}
for sym in ('MA', 'EG'):
    d = load(sym, PARTS)
    n = len(d)
    dt = pd.to_datetime(d.datetime)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    adj = d.adj.values
    segv = d.seg.values
    R = {'n_bars': n}

    # ============ A. 覆盖度 ============
    per_td = d.groupby('td').size()
    dow_td = pd.to_datetime(pd.Series(per_td.index)).dt.dayofweek
    exp_td = dow_td.map(EXPECT_TD).values
    anom_td = per_td[per_td.values != exp_td]
    seg_first = d.groupby('seg').datetime.first()
    segsz = d.groupby('seg').size()
    seg_hour = pd.to_datetime(seg_first).dt.hour
    exp_seg = np.array([SEGLEN[x] for x in seg_hour.values])
    bad_seg = segsz[segsz.values != exp_seg]
    R['coverage'] = {
        'dup_bars': int(d.datetime.duplicated().sum()),
        'n_td': int(len(per_td)),
        'td_by_dow': {int(k): int(v) for k, v in dow_td.value_counts().sort_index().items()},
        'anom_td_n': int(len(anom_td)),
        'anom_td_head': [(str(k), int(v), int(pd.to_datetime(pd.Series([k])).dt.dayofweek.iloc[0])) for k, v in list(anom_td.items())[:15]],
        'n_seg': int(len(segsz)),
        'bad_seg_n': int(len(bad_seg)),
        'bad_seg_head': [(str(seg_first.iloc[i]), int(v)) for i, v in list(bad_seg.items())[:12]],
        'year_range': {int(y): [str(g.datetime.min()), str(g.datetime.max())] for y, g in d.groupby('year')},
        'year_bars': {int(y): int(len(g)) for y, g in d.groupby('year')},
        'bad_ohlc': int(((h < np.maximum(o, c)) | (l > np.minimum(o, c))).sum()),
        'nonpos_price': int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum()),
    }

    # ============ B. 换月与跳变 ============
    # B1 会话内 open != 前收盘（主连换月/错价的直接指纹）
    intra = np.zeros(n, bool)
    intra[1:] = adj[1:] & (o[1:] != c[:-1])
    # B2 会话间跳空
    seg_start = np.zeros(n, bool)
    seg_start[1:] = segv[1:] != segv[:-1]
    og = np.full(n, np.nan)
    idx = np.where(seg_start)[0]
    og[idx] = np.log(o[idx] / c[idx - 1])
    ogbp = og * 1e4
    # B3 相邻 bar 收盘价跳变（会话内）
    cr = np.full(n, np.nan)
    cr[1:] = np.log(c[1:] / c[:-1])
    cr_adj = cr.copy()
    cr_adj[~adj] = np.nan
    mad = np.nanmedian(np.abs(cr_adj - np.nanmedian(cr_adj))) * 1.4826
    big_cr = adj & (np.abs(cr) > max(6 * mad, 0.004))     # >0.4% 或 6×MAD
    # B4 OI 日间断档
    oi_day = d.groupby('td').open_interest.mean()
    oi_chg = oi_day.pct_change().abs()
    R['roll'] = {
        'intra_open_ne_prevclose_n': int(intra.sum()),
        'intra_open_ne_prevclose_dates': [str(dt.iloc[i].date()) for i in np.where(intra)[0][:20]],
        'sess_gap_bp': {q: float(np.nanquantile(ogbp, q)) for q in (0.5, 0.9, 0.99, 1.0)},
        'sess_gap_gt40bp_n': int((np.abs(ogbp) > 40).sum()),
        'sess_gap_top_dates': [(str(dt.iloc[i].date()), round(float(ogbp[i]), 1)) for i in idx[np.argsort(-np.abs(ogbp[idx]))][:12]],
        'cr_adj_mad_bp': float(mad * 1e4),
        'big_cr_n': int(big_cr.sum()),
        'big_cr_dates': [(str(dt.iloc[i]), round(float(cr[i] * 1e4), 1)) for i in np.where(big_cr)[0][:12]],
        'oi_day_chg': {q: float(np.nanquantile(oi_chg, q)) for q in (0.5, 0.9, 0.99, 1.0)},
        'oi_top_dates': [(str(k), round(float(v), 3)) for k, v in oi_chg.sort_values(ascending=False).head(8).items()],
    }
    # top 1% 振幅 bar 与异常事件的共现
    amp = d.amp.values
    thr = np.nanquantile(amp, 0.99)
    top_amp = amp >= thr
    ev = intra.copy()
    ev[seg_start] |= (np.abs(ogbp) > 60)[seg_start]
    ev |= big_cr
    R['roll']['top1pct_amp_n'] = int(top_amp.sum())
    R['roll']['top1pct_amp_event_cooccur'] = float((top_amp & ev).sum() / max(top_amp.sum(), 1))
    R['roll']['event_bars_total'] = int(ev.sum())

    # ============ C. 编码有效性 ============
    body = (c >= o).astype(int)
    up = (h > np.maximum(o, c)).astype(int)
    lo = (l < np.minimum(o, c)).astype(int)
    C = {'by_year': {}}
    yrs = sorted(d.year.unique())
    pooled = d.year.values <= 2025
    st_all = d.state.values
    cnt_pool = np.bincount(st_all[pooled], minlength=8) / pooled.sum()
    for y in yrs:
        m = d.year.values == y
        st = st_all[m]
        cnt = np.bincount(st, minlength=8) / m.sum()
        C['by_year'][int(y)] = {
            'p_body1': float(body[m].mean()), 'p_up1': float(up[m].mean()), 'p_lo1': float(lo[m].mean()),
            'H_body': H([1 - body[m].mean(), body[m].mean()]),
            'H_up': H([1 - up[m].mean(), up[m].mean()]),
            'H_lo': H([1 - lo[m].mean(), lo[m].mean()]),
            'state_freq': [round(float(x), 4) for x in cnt],
            'H_state': H(cnt),
            'n_states_gt1pct': int((cnt > 0.01).sum()),
            'tv_vs_pool': float(0.5 * np.abs(cnt - cnt_pool).sum()),
            'zero_amp': int(((h[m] - l[m]) <= 0).sum()),
            'zero_vol': int((d.volume.values[m] <= 0).sum()),
        }
    clv = d.clv.values
    fin = clv[np.isfinite(clv)]
    C['clv_lattice_mass_3vals'] = float(np.isin(fin, [0.0, 0.5, 1.0]).mean())
    C['clv_distinct'] = int(len(np.unique(np.round(fin, 6))))
    rng = h - l
    rng_pos = rng[rng > 0]
    C['range_ticks_median'] = float(np.median(rng_pos))
    C['zero_amp_total'] = int((rng <= 0).sum())
    R['encoding'] = C

    # ============ D. 信号周期扩展 ============
    cs_r = d.groupby('seg').logret.cumsum().values
    cs_a = d.groupby('seg').amp.cumsum().values
    t = np.arange(n)

    def fwd(h, cs):
        t2 = t + h
        res = np.full(n, np.nan)
        m = (t2 < n) & (segv[np.minimum(t2, n - 1)] == segv)
        res[m] = (cs[t2[m]] - cs[t[m]]) / h
        return res

    lv = np.log1p(d.volume.values)
    mtr = d.year.values <= 2025
    mte = d.year.values == 2026
    D = {'horizon': {}}
    for hh in (1, 5, 15, 30, 60):
        fr = fwd(hh, cs_r)
        fa = fwd(hh, cs_a)
        D['horizon'][hh] = {
            'ic_clv_rc_train': rank_ic(clv[mtr], fr[mtr]),
            'ic_clv_rc_holdout': rank_ic(clv[mte], fr[mte]),
            'ic_vol_amp_train': rank_ic(lv[mtr], fa[mtr]),
            'ic_vol_amp_holdout': rank_ic(lv[mte], fa[mte]),
            'ic_vol_rc_train': rank_ic(lv[mtr], fr[mtr]),
            'ic_clv_amp_train': rank_ic(clv[mtr], fa[mtr]),
        }
    # 会话级：夜盘→当日、日级自相关
    day_mask = d.slot.values < 225

    def amp_of(mask):
        sub = d[mask]
        g = sub.groupby('td')
        return (g.high.max() - g.low.min()) / g.open.first() * 100

    day_amp = amp_of(day_mask)
    night_amp = amp_of(~day_mask)
    common = day_amp.dropna().index.intersection(night_amp.dropna().index)
    cdf = pd.DataFrame({'y': pd.to_datetime(pd.Series(common)).dt.year.values,
                        'night': night_amp[common].values, 'day': day_amp[common].values})
    D['night_to_day'] = {}
    for y in sorted(cdf.y.unique()):
        sub = cdf[cdf.y == y]
        D['night_to_day'][int(y)] = {
            'n': int(len(sub)),
            'ic_night_day': rank_ic(sub.night.values, sub.day.values),
            'night_mean': float(sub.night.mean()), 'day_mean': float(sub.day.mean()),
        }
    # 日级振幅自相关（交易日序列，跳过无日盘的周六）
    da = day_amp.dropna().sort_index()
    s = pd.Series(da.values)
    D['day_acf'] = {f'acf{k}': float(s.autocorr(k)) for k in (1, 2, 3, 5)}
    da_y = pd.Series(da.values, index=pd.to_datetime(pd.Series(da.index)).dt.year.values)
    D['day_lag1_ic_by_year'] = {}
    for y in sorted(da_y.index.unique()):
        v = da_y[da_y.index == y].values
        D['day_lag1_ic_by_year'][int(y)] = rank_ic(v[:-1], v[1:]) if len(v) > 10 else None
    R['signal'] = D
    out[sym] = R
    print(f'--- {sym} done: {n:,} bars, {len(per_td)} tds, {len(segsz)} segs')

import json
with open('results_gap.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print('saved results_gap.json')
