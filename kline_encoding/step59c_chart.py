# -*- coding: utf-8 -*-
"""step59c —— EG 2026 价格图：标注 Route B(偏离回归) 的实际入场/出场，直观看"大波动里做回归"的代价。"""
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
from step46_prod_bt import load_all, build_cost_fn, build_sym
from step59_ab_engines import build_signals, run_engine

SYM = 'EG888'
CFG_B = dict(DEV=2.5, SL_ATR=2.5, TRAIL_ATR=2.5)
ba, raw, dd, var, cost, fac = load_all()
costmap = build_cost_fn(cost, var)
b = build_sym(SYM, ba, raw, dd, var)
b = b.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
vc = SYM[:-3]
sig, atr, e140 = build_signals(b, 'B', CFG_B)
tr = run_engine(SYM, b, costmap[vc], 1.0, sig, atr, e140, 'B', CFG_B)
df = pd.DataFrame(tr)
df['xdt'] = pd.to_datetime(df.xdt)
df['edt'] = pd.to_datetime(df.edt)
df = df[(df.year == 2026) & (df.xdt >= '2026-09-01')].copy()

bb = b[b.ts >= '2026-09-01'].copy()
bb['ts'] = pd.to_datetime(bb.ts)
fig, ax = plt.subplots(figsize=(13, 5.5))
ax.plot(bb.ts, bb.close, color='#7d8aa0', lw=1.0, label='EG close (60m)')
# 高波日阴影
hv = bb[bb.hvol]
ax.scatter(hv.ts, hv.close, s=6, color='#5a3a3a', alpha=0.25, zorder=0)
for _, t in df.iterrows():
    col = '#37d67a' if t.net > 0 else '#ff5b5b'
    ax.scatter(t.edt, t.P, marker='^' if t.dir == 1 else 'v', s=42, color=col, zorder=5)
    ax.plot([t.edt, t.xdt], [t.P, t.X], color=col, lw=0.8, alpha=0.7, zorder=4)
ax.set_title(f'EG 2026-09~10  Route B (deviation-reversion) actual trades  '
             f'n={len(df)}  net={df.net.sum():.0f}  win={(df.net>0).mean():.0%}', color='#e6edf3')
ax.set_ylabel('close', color='#8b98a5')
ax.xaxis.set_major_formatter(DateFormatter('%m-%d'))
ax.tick_params(colors='#8b98a5')
for sp in ['top', 'right']:
    ax.spines[sp].set_visible(False)
ax.grid(alpha=0.12)
from matplotlib.lines import Line2D
leg = [Line2D([0], [0], marker='^', color='w', markerfacecolor='#37d67a', markersize=8, label='long entry (green=win, red=loss)'),
       Line2D([0], [0], marker='v', color='w', markerfacecolor='#ff5b5b', markersize=8, label='short entry'),
       Line2D([0], [0], ls='', marker='o', color='w', markerfacecolor='#5a3a3a', markersize=5, label='high-vol day (P2 would block)')]
ax.legend(handles=leg, loc='upper left', fontsize=8, framealpha=0.85)
fig.text(0.5, 0.01, 'Green/red triangles = Route B entry (color=outcome); line=held to exit. '
         'Note how fades fire INTO the Sept-Oct rally and mostly lose.', ha='center', color='#8b98a5', fontsize=8)
fig.savefig('step59_signals_2026.png', dpi=110, facecolor='#0d1117')
print('saved step59_signals_2026.png  n=', len(df), 'net=', round(df.net.sum(), 1))
