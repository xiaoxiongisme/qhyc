import pandas as pd, numpy as np, os
OUT = '/tmp/roll_out'
b = pd.read_csv(os.path.join(OUT, 'a_trades_2_1.0.csv'), encoding='utf-8-sig')
d = pd.read_csv(os.path.join(OUT, 'a_trades_3_1.0.csv'), encoding='utf-8-sig')
print('[逐年]   add_max=2  vs  add_max=3')
ys = sorted(set(b.eyear) | set(d.eyear))
print('%-6s %12s %12s %12s' % ('年', '2手', '3手', 'Δ'))
for y in ys:
    a = float(b[b.eyear == y].pnl.sum()); c = float(d[d.eyear == y].pnl.sum())
    print('%-6s %12.0f %12.0f %+12.0f' % (y, a, c, c - a))
print('%-6s %12.0f %12.0f %+12.0f' % ('合计', b.pnl.sum(), d.pnl.sum(), d.pnl.sum() - b.pnl.sum()))
print('\n[去TOP品种]  用 add_max=2 口径的 top 品种从两边同时剔除')
rb = b.groupby('symbol').pnl.sum().sort_values(ascending=False)
print('%-8s %14s %14s %14s' % ('剔除', '2手', '3手', 'Δ'))
for k in (1, 3, 5):
    drop = set(rb.head(k).index)
    a = float(b[~b.symbol.isin(drop)].pnl.sum()); c = float(d[~d.symbol.isin(drop)].pnl.sum())
    print('%-8s %14.0f %14.0f %+14.0f' % ('top%d' % k, a, c, c - a))
print('\n[品种聚类 bootstrap 2000次 Δ=3手−2手]')
rng = np.random.default_rng(20261007)
syms = np.array(sorted(set(b.symbol) | set(d.symbol)))
gb = b.groupby('symbol').pnl.sum().reindex(syms).fillna(0).to_numpy()
gd = d.groupby('symbol').pnl.sum().reindex(syms).fillna(0).to_numpy()
dd = gd - gb; NS = len(syms)
s = dd[rng.integers(0, NS, size=(2000, NS))].sum(axis=1)
print('  点估计 %+.0f  中位 %+.0f  5%%分位 %+.0f  P(Δ>0) %.1f%%'
      % (dd.sum(), np.median(s), np.percentile(s, 5), 100 * (s > 0).mean()))
print('\n[持仓天数结构 × 底仓/加仓]')
for nm, x in (('2手', b), ('3手', d)):
    for lo, hi in ((0, 3), (3, 10), (10, 31)):
        g = x[(x.hold_days >= lo) & (x.hold_days < hi)]
        ga = g[g.lot_kind == 'addon']; gb2 = g[g.lot_kind == 'base']
        print('  %-4s %d-%-2d  底仓%5d %+10.0f | 加仓%5d %+10.0f'
              % (nm, lo, hi, len(gb2), gb2.pnl.sum(), len(ga), ga.pnl.sum()))
print('\n[成本]  2手 %.0f (占毛利 %.1f%%)   3手 %.0f (占毛利 %.1f%%)'
      % (b.cost.sum(), 100 * b.cost.sum() / (b.pnl.sum() + b.cost.sum()),
         d.cost.sum(), 100 * d.cost.sum() / (d.pnl.sum() + d.cost.sum())))
