# -*- coding: utf-8 -*-
"""绘制 EB/EG 2026 价格 + 高波日阴影 + 每个 raw 信号标记（成交=金星，被拒=三角）。
直观回答：为什么大波动期反而 0 成交。"""
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CostCfg, DailyCtx, SymbolEngine, CLUSTER

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
data = {}
for s in ['EB888', 'EG888']:
    b = s46.build_sym(s, ba, raw, dd, var)
    if b is not None:
        data[s] = b
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)
bm = {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}


def run(sym):
    b = data[sym]; vc = sym[:-3]
    fee_row, slip, mult = costmap[vc]
    cfg = V5XConfig(); cfg.DIAGNOSE = True; cfg.ALT_GATE = 'none'
    eng = SymbolEngine(sym, cfg, CostCfg(fee_row, slip, mult, tick.get(vc, 1.0)))
    sl = frozenset(int(x) for x in b.loc[b.sess_last, 'hour'].unique())
    prev = [None]
    for row in b.itertuples():
        if row.td != prev[0]:
            eng.set_daily_ctx(DailyCtx(td=row.td, hvol=bool(row.hvol), member_z=np.nan,
                                       blocked=row.td in bm[sym], sess_last_hours=sl))
            prev[0] = row.td
        eng.on_bar(row.ts, float(row.open), float(row.high), float(row.low),
                   float(row.close), float(row.volume), float(row.raw_c), allow=bool(row.allow))
    return b, eng


fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True)
NAVY = '#0f1620'; RED = '#ff5b5b'; GRN = '#26a69a'; GOLD = '#ffd54a'
fig.patch.set_facecolor(NAVY)
for ax in axes:
    ax.set_facecolor(NAVY)

for ax, sym in zip(axes, ['EB888', 'EG888']):
    b, eng = run(sym)
    b = b.copy()
    b['dt'] = pd.to_datetime(b.ts)
    m2026 = b['dt'].dt.year == 2026
    bb = b[m2026].reset_index(drop=True)
    ax.plot(bb['dt'], bb['close'], color='#9fb3c8', lw=0.7)
    # 高波日阴影
    hv_days = pd.to_datetime(bb.loc[bb.hvol, 'dt'].dt.date.unique())
    for d in hv_days:
        ax.axvspan(d, d + pd.Timedelta(days=1), color=RED, alpha=0.10, lw=0)
    # 信号
    rl = [(pd.to_datetime(t), d, tr) for (t, d, tr) in eng._raw_list if pd.to_datetime(t).year == 2026]
    for t, d, tr in rl:
        y = bb['close'].iloc[(bb['dt'] - t).abs().argmin()]
        if tr:
            ax.scatter(t, y, marker='*', s=180, color=GOLD, zorder=5, edgecolor='k', linewidth=0.4)
        else:
            ax.scatter(t, y, marker='^' if d > 0 else 'v', s=55,
                       color=GRN if d > 0 else RED, zorder=4, alpha=0.9)
    raw = len(rl); traded = sum(1 for _, _, tr in rl if tr)
    killed = raw - traded
    ax.set_title(f'{sym} 2026  (red band = high-vol day, no-open; tri=signal candidate, star=filled trade)   '
                 f'candidates {raw} -> gated out {killed} -> trades {traded}', color='#e6edf3', fontsize=11)
    ax.tick_params(colors='#8b98a5')
    ax.spines['bottom'].set_color('#2a3340'); ax.spines['top'].set_color('#2a3340')
    ax.spines['left'].set_color('#2a3340'); ax.spines['right'].set_color('#2a3340')
    ax.yaxis.label.set_color('#8b98a5'); ax.xaxis.label.set_color('#8b98a5')
    ax.set_ylabel('Close', color='#8b98a5')

axes[1].xaxis.set_major_formatter(DateFormatter('%m-%d'))
fig.suptitle('Why V5X is nearly dormant on EB/EG in 2026: candidate signals land mostly on high-vol days '
             '(P2 no-open) or are rejected by the MACD-histogram gate (M1a)', color='#e6edf3', fontsize=12)
fig.text(0.5, 0.02, 'Data through 2026-10-16. Red band = daily range > 80th pct of 250d (P2 blocks opening).',
         color='#8b98a5', ha='center', fontsize=9)
fig.tight_layout(rect=[0, 0.04, 1, 0.95])
fig.savefig('step58_signals_2026.png', dpi=110, facecolor=NAVY)
print('saved step58_signals_2026.png')
print('EB raw/traded:', sum(1 for t,d,tr in [(pd.to_datetime(t),d,tr) for (t,d,tr) in run("EB888")[1]._raw_list if pd.to_datetime(t).year==2026]),
      '/', sum(1 for t,d,tr in [(pd.to_datetime(t),d,tr) for (t,d,tr) in run("EB888")[1]._raw_list if pd.to_datetime(t).year==2026] if tr))
