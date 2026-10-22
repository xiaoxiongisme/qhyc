# -*- coding: utf-8 -*-
"""S1 微观结构深挖。
(a) 日内已实现波动/成交量 U 型（按 slot 池化，周一交互，|r| 与 amp 双口径）
(b) 订单流不平衡代理 clv=(C-L)/(H-L)：分布、对下一根收益/振幅预测力、累计 OFI 持续性
(c) 开盘集中度：前 15/30 分钟占会话振幅/成交量比例
(d) 量价状态特征：各八卦态成交量、clv 离散度、volume→amp 预测
(e) 周一效应双口径：逐根 |r|/amp vs 日振幅（与 D4 dow_mean 互证）
口径：会话内 60 秒相邻（adj），夜盘归属次日交易日；预测类指标 train(2020-2025)/holdout(2026) 分开。
"""
import numpy as np, pandas as pd
from ke_common import load, spearman, save_json, NAMES

PARTS = ['2020', '2021_2025', '2026']
SEGLEN = {9: 75, 10: 60, 13: 90, 21: 120}     # 各时段实际 bar 数


def safe_mean(a):
    a = np.asarray(a, float)
    a = a[~np.isnan(a)]
    return float(a.mean()) if len(a) else float('nan')


RES = {}
for sym in ['MA', 'EG']:
    d = load(sym, PARTS)
    n = len(d)
    m_tr = d.year.values < 2026
    m_te = ~m_tr
    vol = d.volume.values.astype(float)
    cov = {'n': int(n), 'n_train': int(m_tr.sum()), 'n_holdout': int(m_te.sum()),
           'vol_zero_frac': float((vol <= 0).mean()),
           'years': {int(y): int((d.year.values == y).sum()) for y in range(2020, 2027)},
           'amp_nan': int(np.isnan(d.amp.values).sum()), 'clv_nan': int(np.isnan(d.clv.values).sum())}
    amp, clv, st = d.amp.values, d.clv.values, d.state.values
    c = d.close.values
    adj = d.adj.values
    rc = np.full(n, np.nan)                      # 会话内 close-to-close 对数收益
    rc[adj] = np.log(c[adj] / c[np.where(adj)[0] - 1])
    slot, seq, seg = d.slot.values, d.seq.values, d.seg.values
    dow = d.dow.values
    seg_hour = d.groupby('seg').datetime.first().dt.hour
    seg_hour = d.seg.map(seg_hour).values

    # ---------- (a) 日内 U 型 ----------
    absr = np.abs(rc)
    prof_absr = np.full(360, np.nan); prof_amp = np.full(360, np.nan)
    vprof = np.full(360, np.nan); cnt_slot = np.zeros(360)
    for s in range(360):
        m = slot == s
        cnt_slot[s] = m.sum()
        if m.sum() > 0:
            prof_absr[s] = safe_mean(absr[m]); prof_amp[s] = safe_mean(amp[m]); vprof[s] = vol[m].mean()
    part = np.full(n, -1)
    for hr, L in SEGLEN.items():
        m = seg_hour == hr
        part[m & (seq < 15)] = 0
        part[m & (seq >= 15) & (seq < L - 15)] = 1
        part[m & (seq >= L - 15)] = 2
    uprof = {}
    for p, nm in [(0, 'first15'), (1, 'mid'), (2, 'last15')]:
        m = (part == p) & adj
        uprof[nm] = {'mean_absr_bp': safe_mean(absr[m]) * 1e4,
                     'mean_amp': safe_mean(amp[m]),
                     'n': int(m.sum()), 'mean_vol': safe_mean(vol[part == p])}
    # 周一交互：周一 vs 周二~五（仅日盘）
    dow_m = (dow == 0) & (slot < 225) & adj
    dow_o = (dow > 0) & (dow < 5) & (slot < 225) & adj
    mon_ratio_slot = np.full(135, np.nan)
    mon_ratio_amp_slot = np.full(135, np.nan)
    for s in range(135):
        a = absr[dow_m & (slot == s)]; b = absr[dow_o & (slot == s)]
        a2 = amp[dow_m & (slot == s)]; b2 = amp[dow_o & (slot == s)]
        if len(a) > 50 and len(b) > 50 and safe_mean(b) > 0:
            mon_ratio_slot[s] = safe_mean(a) / safe_mean(b)
        if len(a2) > 50 and len(b2) > 50 and safe_mean(b2) > 0:
            mon_ratio_amp_slot[s] = safe_mean(a2) / safe_mean(b2)
    mon = {'monday_absr_bp': safe_mean(absr[dow_m]) * 1e4, 'other_absr_bp': safe_mean(absr[dow_o]) * 1e4,
           'ratio_absr': safe_mean(absr[dow_m]) / safe_mean(absr[dow_o]),
           'ratio_first30': safe_mean(mon_ratio_slot[:30]),
           'ratio_rest': safe_mean(mon_ratio_slot[30:]),
           'ratio_amp': safe_mean(amp[dow_m]) / safe_mean(amp[dow_o]),
           'ratio_amp_first30': safe_mean(mon_ratio_amp_slot[:30]),
           'ratio_amp_rest': safe_mean(mon_ratio_amp_slot[30:]),
           'mon_ratio_slot': [None if np.isnan(x) else round(float(x), 3) for x in mon_ratio_slot],
           'mon_ratio_amp_slot': [None if np.isnan(x) else round(float(x), 3) for x in mon_ratio_amp_slot]}
    intraday = {'prof_absr_bp': [None if np.isnan(x) else round(float(x) * 1e4, 3) for x in prof_absr],
                'prof_amp': [None if np.isnan(x) else round(float(x), 5) for x in prof_amp],
                'vprof': [None if np.isnan(x) else round(float(x), 1) for x in vprof],
                'cnt_slot': cnt_slot.astype(int).tolist(),
                'uprof': uprof, 'monday': mon}

    # ---------- (e) 周一双口径：日振幅 ----------
    td_dt = pd.to_datetime(pd.Series(d.td.values))
    grp = d.groupby(td_dt.values)
    day_rng = (grp.high.max() - grp.low.min()) / grp.open.first() * 100.0
    day_dow = td_dt.dt.dayofweek.groupby(td_dt.values).first()
    tr_day = pd.Series(m_tr).groupby(td_dt.values).any()
    dd = pd.DataFrame({'amp_day': day_rng.values, 'dow': day_dow.values, 'is_tr': tr_day.values})
    dow_day_tr = dd[(dd.dow < 5) & dd.is_tr].groupby('dow').amp_day.mean()
    # D4 口径复验：5 分钟块振幅 → 用 1 分钟 bar 振幅按星期平均（train）
    dow_amp_1m = [safe_mean(amp[(dow == w) & m_tr]) for w in range(5)]
    # 干净口径：只用日盘（slot<225）比星期；日级也只用日盘 bar 算当日振幅
    dow_amp_day = [safe_mean(amp[(dow == w) & m_tr & (slot < 225)]) for w in range(5)]
    dday = d[slot < 225]
    td_d = pd.to_datetime(pd.Series(dday.td.values))
    gd = dday.groupby(td_d.values)
    drng = (gd.high.max() - gd.low.min()) / gd.open.first() * 100.0
    ddow = td_d.dt.dayofweek.groupby(td_d.values).first()
    dtr = pd.Series(m_tr[slot < 225]).groupby(td_d.values).any()
    ddf = pd.DataFrame({'r': drng.values, 'dow': ddow.values, 'tr': dtr.values})
    dow_dr_day = ddf[(ddf.dow < 5) & ddf.tr].groupby('dow').r.mean()
    monday_day = {'dow_mean_train': [float(x) for x in dow_day_tr.values],
                  'mon_vs_other_train': float(dow_day_tr.values[0] / dow_day_tr.values[1:].mean()),
                  'dow_amp_1m_train': dow_amp_1m,
                  'mon_vs_other_amp_1m': float(dow_amp_1m[0] / np.mean(dow_amp_1m[1:])),
                  'dow_amp_dayonly_train': dow_amp_day,
                  'mon_vs_other_amp_dayonly': float(dow_amp_day[0] / np.mean(dow_amp_day[1:])),
                  'dow_dayrange_dayonly_train': [float(x) for x in dow_dr_day.values],
                  'mon_vs_other_dayrange_dayonly': float(dow_dr_day.values[0] / dow_dr_day.values[1:].mean()),
                  'n_sunday_bars': int((d.datetime.dt.dayofweek == 6).sum()),
                  'monday_td_night_bars': int(((dow == 0) & (slot >= 225)).sum())}

    # ---------- (b) OFI 代理 ----------
    idx = np.where(adj)[0]
    t0 = idx - 1
    x_clv = clv[t0]
    y_rc, y_amp = rc[idx], amp[idx]
    mtr = m_tr[t0]; mte = m_te[t0]
    ofi = {'clv_mean': safe_mean(x_clv), 'clv_sd': float(np.nanstd(x_clv)),
           'clv_hist': np.histogram(x_clv, bins=10, range=(0, 1))[0].tolist()}
    ofi['ic_rc_train'] = spearman(x_clv[mtr], y_rc[mtr])
    ofi['ic_rc_holdout'] = spearman(x_clv[mte], y_rc[mte])
    ofi['ic_amp_train'] = spearman(x_clv[mtr], y_amp[mtr])
    ofi['ic_amp_holdout'] = spearman(x_clv[mte], y_amp[mte])
    qs = np.quantile(x_clv[mtr], [0.2, 0.4, 0.6, 0.8])
    qb = np.clip(np.digitize(x_clv, qs), 0, 4)
    rows = []
    for q in range(5):
        m = (qb == q) & mtr
        rows.append({'q': q, 'n': int(m.sum()),
                     'next_rc_bp': safe_mean(y_rc[m]) * 1e4,
                     'next_amp': safe_mean(y_amp[m]),
                     'cont_rate': safe_mean(np.sign(y_rc[m]) == np.sign(x_clv[m] - 0.5))})
    ofi['quintiles'] = rows
    # 累计 OFI（过去 k 根 clv-0.5 均值）→ 未来 k 根收益；fwd 用会话内 cumsum 差分，cofi 按会话 rolling
    imb = clv - 0.5
    rcf = np.where(np.isnan(rc), 0.0, rc)
    cs = np.concatenate([[0.0], np.cumsum(rcf)[:-1]])   # cs[p] = sum(rc[:p])
    imb_s = pd.Series(imb)
    cum = {}
    for k in [5, 15, 60]:
        cofi = imb_s.groupby(seg).transform(lambda s: s.rolling(k, min_periods=k).mean()).values
        same = np.zeros(n, bool)
        same[:n - k] = seg[:n - k] == seg[k:]
        fwd = np.full(n, np.nan)
        fwd[:n - k][same[:n - k]] = (cs[k:] - cs[:n - k])[same[:n - k]]
        mm = adj & ~np.isnan(fwd) & ~np.isnan(cofi)
        cum[str(k)] = {'n': int(mm.sum()), 'n_holdout': int((mm & m_te).sum()),
                       'ic_train': spearman(cofi[mm & m_tr], fwd[mm & m_tr]),
                       'ic_holdout': spearman(cofi[mm & m_te], fwd[mm & m_te])}
    ofi['cum_ofi'] = cum
    lv = np.log1p(vol)
    ofi['vol_amp_ic_train'] = spearman(lv[t0][mtr], y_amp[mtr])
    ofi['vol_amp_ic_holdout'] = spearman(lv[t0][mte], y_amp[mte])
    ofi['vol_rc_ic_train'] = spearman(lv[t0][mtr], y_rc[mtr])
    ofi['vol_rc_ic_holdout'] = spearman(lv[t0][mte], y_rc[mte])
    # 1 分钟收益自相关：仅用真正相邻对 (rc[t], rc[t+1])，adj[t0] 保证 rc[t0] 有定义
    m2 = adj[t0]
    ofi['acf1_rc_train'] = float(np.corrcoef(rc[t0][m2 & mtr], y_rc[m2 & mtr])[0, 1])
    ofi['acf1_rc_holdout'] = float(np.corrcoef(rc[t0][m2 & mte], y_rc[m2 & mte])[0, 1])

    # ---------- (c) 开盘集中度 ----------
    conc = {}
    for hr in SEGLEN:
        m = seg_hour == hr
        rng = np.full(n, np.nan); v15 = np.full(n, np.nan); v30 = np.full(n, np.nan)
        for s in np.unique(seg[m]):
            ii = np.where(seg == s)[0]
            if len(ii) < 60: continue
            hh, ll, vv = d.high.values[ii], d.low.values[ii], vol[ii]
            rng[ii[0]] = hh.max() - ll.min()
            k15 = min(15, len(ii)); k30 = min(30, len(ii))
            v15[ii[0]] = (hh[:k15].max() - ll[:k15].min()) / max(rng[ii[0]], 1e-9)
            v30[ii[0]] = vv[:k30].sum() / max(vv.sum(), 1e-9)
        mm = ~np.isnan(rng)
        conc[str(hr)] = {'n_sessions': int(mm.sum()),
                         'range_share_first15': safe_mean(v15[mm]),
                         'vol_share_first30': safe_mean(v30[mm])}

    # ---------- (d) 量价状态特征 ----------
    vol_med_slot = np.zeros(360)
    for s in range(360):
        m = slot == s
        if m.sum() > 30: vol_med_slot[s] = np.median(vol[m])
    vr = np.where(vol_med_slot[slot] > 0, vol / np.maximum(vol_med_slot[slot], 1e-9), np.nan)
    state_vol = []
    for s in range(8):
        m = st == s
        state_vol.append({'state': NAMES[s], 'n': int(m.sum()),
                          'vol_rel': safe_mean(vr[m]),
                          'clv_disp': safe_mean(np.abs(clv[m] - 0.5)),
                          'amp': safe_mean(amp[m])})
    qs_amp = np.quantile(amp[m_tr], [0.2, 0.4, 0.6, 0.8])
    ampq = np.clip(np.digitize(amp, qs_amp), 0, 4)
    vol_by_ampq = [safe_mean(vr[ampq == q]) for q in range(5)]

    RES[sym] = {'coverage': cov, 'intraday': intraday, 'ofi': ofi, 'monday_day': monday_day,
                'concentration': conc, 'state_vol': state_vol, 'vol_by_ampq': vol_by_ampq}
    print(f'[{sym}] n={n} train={cov["n_train"]} holdout={cov["n_holdout"]} vol0={cov["vol_zero_frac"]:.4f}')
    print('  U型 |r|bp first15/mid/last15: %.2f/%.2f/%.2f  amp: %.4f/%.4f/%.4f' % (
        uprof['first15']['mean_absr_bp'], uprof['mid']['mean_absr_bp'], uprof['last15']['mean_absr_bp'],
        uprof['first15']['mean_amp'], uprof['mid']['mean_amp'], uprof['last15']['mean_amp']))
    print('  周一 |r|比 %.3f (前30slot %.3f/其余 %.3f) amp比 %.3f (前30 %.3f/其余 %.3f)' % (
        mon['ratio_absr'], mon['ratio_first30'], mon['ratio_rest'],
        mon['ratio_amp'], mon['ratio_amp_first30'], mon['ratio_amp_rest']))
    print('  周一口径互证: 全bar 1min振幅 %.3f | 日盘only 1min振幅 %.3f | 日盘only 日振幅 %.3f (周日bar=%d, 周一td夜盘bar=%d)' % (
        monday_day['mon_vs_other_amp_1m'], monday_day['mon_vs_other_amp_dayonly'],
        monday_day['mon_vs_other_dayrange_dayonly'], monday_day['n_sunday_bars'], monday_day['monday_td_night_bars']))
    print('  dow_amp_dayonly(train):', [round(x, 4) for x in monday_day['dow_amp_dayonly_train']])
    print('  OFI IC: rc %.4f→%.4f | amp %.4f→%.4f (train→holdout)' % (
        ofi['ic_rc_train'], ofi['ic_rc_holdout'], ofi['ic_amp_train'], ofi['ic_amp_holdout']))
    print('  累计OFI IC holdout(5/15/60根):', {k: (v['n_holdout'], round(v['ic_holdout'], 4)) for k, v in cum.items()})
    print('  vol→amp IC %.4f→%.4f | vol→rc IC %.4f→%.4f | acf1 %.4f→%.4f' % (
        ofi['vol_amp_ic_train'], ofi['vol_amp_ic_holdout'], ofi['vol_rc_ic_train'], ofi['vol_rc_ic_holdout'],
        ofi['acf1_rc_train'], ofi['acf1_rc_holdout']))
    print('  开盘集中度(range15/vol30):', {k: (round(v['range_share_first15'], 3), round(v['vol_share_first30'], 3)) for k, v in conc.items()})
    print('  状态量比/clv离散:', [(r['state'], round(r['vol_rel'], 2), round(r['clv_disp'], 3)) for r in state_vol])
    print('  amp分位→量比:', [round(x, 2) for x in vol_by_ampq])

save_json(RES, 'results_micro.json')
print('saved results_micro.json')
