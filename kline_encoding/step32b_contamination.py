# -*- coding: utf-8 -*-
"""Step 32b: 2026 春季极端行情对既有结论的污染复核
1. EG 38 个会话内 open≠前收 事件的日期/量级定性
2. U 型与开盘集中度：剔除事件会话（跳空>60bp）后重算，检验结论稳健性
3. 2026 春季行情画像：日线级别路径
"""
import sys; sys.path.insert(0, '.')
import numpy as np, pandas as pd
from ke_common import load

PARTS = ['2020', '2021_2025', '2026']

for sym in ('MA', 'EG'):
    d = load(sym, PARTS)
    n = len(d)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    adj = d.adj.values
    segv = d.seg.values
    dt = pd.to_datetime(d.datetime)

    # --- 1. 会话内 open≠前收 事件 ---
    intra = np.zeros(n, bool)
    intra[1:] = adj[1:] & (o[1:] != c[:-1])
    if intra.sum():
        idx = np.where(intra)[0]
        gaps = np.log(o[idx] / c[idx - 1]) * 1e4
        yrs = dt.iloc[idx].dt.year.values
        print(f'=== {sym} 会话内 open≠前收: {len(idx)} 个')
        print('  年份分布:', {int(y): int((yrs == y).sum()) for y in sorted(set(yrs))})
        print('  跳空量级 bp: min {:.0f} max {:.0f} |均值| {:.1f}'.format(gaps.min(), gaps.max(), np.abs(gaps).mean()))
        print('  前 5 个:', [(str(dt.iloc[i]), round(float(np.log(o[i] / c[i - 1]) * 1e4), 1)) for i in idx[:5]])

    # --- 2. 事件会话定义与 U 型复核 ---
    seg_start = np.zeros(n, bool)
    seg_start[1:] = segv[1:] != segv[:-1]
    og = np.full(n, np.nan)
    idx2 = np.where(seg_start)[0]
    og[idx2] = np.log(o[idx2] / c[idx2 - 1]) * 1e4
    ev_seg = np.zeros(n, bool)
    ev_seg[seg_start] = (np.abs(og) > 60)[seg_start]
    # 把事件标记广播到整个会话
    ev_seg_full = pd.Series(ev_seg).groupby(segv).transform('any').values.astype(bool)
    print(f'=== {sym} 事件会话（开盘跳空>60bp）: {len(set(segv[ev_seg_full]))} / {segv.max() + 1}')

    # 每会话开盘集中度：前 15 根 range / 全会话 range
    df = pd.DataFrame({'seg': segv, 'h': h, 'l': l, 'ev': ev_seg_full, 'seq': d.seq.values})
    seg_rng = df.groupby('seg').apply(lambda g: g.h.max() - g.l.min(), include_groups=False)
    first15 = df[df.seq < 15].groupby('seg').apply(lambda g: g.h.max() - g.l.min(), include_groups=False)
    share = (first15 / seg_rng).dropna()
    ev_flag = df.groupby('seg').ev.first()
    for lbl, m in (('全部会话', np.ones(len(share), bool)), ('剔除事件会话', ~ev_flag.loc[share.index].values)):
        print(f'  first15 振幅占比 [{lbl}]: mean {share.values[m].mean():.4f} median {np.median(share.values[m]):.4f}')
    # |r| 分时段均值
    absr = np.abs(d.logret.values)
    slot = d.slot.values
    m_ev = ev_seg_full
    for lbl, m in (('全部', np.ones(n, bool)), ('剔除事件', ~m_ev)):
        a = absr[m & (slot < 15)].mean() * 1e4
        b = absr[m & (slot >= 75) & (slot < 225)].mean() * 1e4
        cc = absr[m & (slot >= 330)].mean() * 1e4
        print(f'  |r| bp first15/mid/last15 [{lbl}]: {a:.2f} / {b:.2f} / {cc:.2f}')

    # --- 3. 2026 春季日线画像 ---
    d26 = d[d.year == 2026]
    daily = d26.groupby(d26.datetime.dt.date).apply(
        lambda g: pd.Series({'open': g.open.iloc[0], 'high': g.high.max(), 'low': g.low.min(),
                             'close': g.close.iloc[-1]}), include_groups=False)
    daily.index = pd.to_datetime(daily.index)
    daily = daily.loc['2026-03-01':'2026-04-30']
    print(f'=== {sym} 2026-03~04 日线（部分）:')
    print(daily.iloc[::4].round(0).to_string())
