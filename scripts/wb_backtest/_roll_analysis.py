# -*- coding: utf-8 -*-
"""滚动品种池筛选结果解剖：逐年 / 去 TOP 品种 / 品种聚类 bootstrap / 池子换手。"""
import os
import json
import numpy as np
import pandas as pd

OUT = '/tmp/roll_out'
RS = json.load(open(os.path.join(OUT, 'results.json'), encoding='utf-8'))
RES = RS['results']
NAMES = list(RES.keys())


def load(n):
    p = os.path.join(OUT, 'trades_%s.csv' % n)
    return pd.read_csv(p, encoding='utf-8-sig') if os.path.exists(p) else None


D = {n: load(n) for n in NAMES}
BASE = D['BASE_nofilter']
print('=' * 100)
print('OOS %s  events=%d  参考跑(全历史全品种) n=%d net=%.0f'
      % (RS['cut'], RS['oos_events'], RS['ref_n'], RS['ref_net']))
print('=' * 100)

# ---------- 1. 总表 ----------
print('\n[1] 总表（净，元）')
print('%-16s %6s %11s %6s %6s %7s %11s %11s %11s'
      % ('变体', '笔数', '净', 'PF', '胜率', '单笔', '峰值保证金', 'MDD', '建议资金'))
for n in NAMES:
    m = RES[n]
    print('%-16s %6d %11.0f %6.3f %5.1f%% %7.0f %11.0f %11.0f %11.0f'
          % (n, m['n'], m['net'], m['pf'], m['win'], m['per_trade'],
             m['peak_margin'], m['mdd'], m['suggest_capital']))

# ---------- 2. 逐年 ----------
print('\n[2] 逐年净收益（元）')
years = sorted(set(BASE.eyear))
hdr = '%-16s' % '变体' + ''.join('%12s' % y for y in years) + '%12s' % '合计'
print(hdr)
for n in NAMES:
    s = D[n].groupby('eyear').pnl.sum()
    row = '%-16s' % n + ''.join('%12.0f' % s.get(y, 0.0) for y in years)
    print(row + '%12.0f' % D[n].pnl.sum())
# 年胜出计数
print('\n  逐年「优于不筛」计数：')
for n in NAMES[1:]:
    s = D[n].groupby('eyear').pnl.sum()
    b = BASE.groupby('eyear').pnl.sum()
    win = sum(1 for y in years if s.get(y, 0.0) > b.get(y, 0.0))
    print('    %-16s %d/%d 年优于基线' % (n, win, len(years)))

# ---------- 3. 去 TOP 品种 ----------
print('\n[3] 剔除贡献最大品种后的净收益（元）  基线 TOP5：%s'
      % [(d['s'], d['v']) for d in RES['BASE_nofilter']['top5']])
base_rank = BASE.groupby('symbol').pnl.sum().sort_values(ascending=False)
print('%-16s %11s %11s %11s %11s' % ('变体', '原始', '剔top1', '剔top3', '剔top5'))
for n in NAMES:
    df = D[n]
    r = df.groupby('symbol').pnl.sum().sort_values(ascending=False)
    vals = []
    for k in (1, 3, 5):
        drop = set(r.head(k).index)
        vals.append(float(df[~df.symbol.isin(drop)].pnl.sum()))
    print('%-16s %11.0f %11.0f %11.0f %11.0f'
          % (n, float(df.pnl.sum()), vals[0], vals[1], vals[2]))

# ---------- 4. 品种聚类 bootstrap：差值是否显著 ----------
print('\n[4] 品种聚类 bootstrap（对品种有放回重抽样 2000 次，统计 变体−基线 的净差）')
rng = np.random.default_rng(20261007)
syms = np.array(sorted(BASE.symbol.unique()))
gbase = BASE.groupby('symbol').pnl.sum().reindex(syms).fillna(0.0).to_numpy()
NS = len(syms)
B = 2000
print('%-16s %10s %10s %10s %8s' % ('变体', '点估计', '中位', '5%分位', 'P(>0)'))
diff_rows = {}
for n in NAMES[1:]:
    gv = D[n].groupby('symbol').pnl.sum().reindex(syms).fillna(0.0).to_numpy()
    d = gv - gbase
    idx = rng.integers(0, NS, size=(B, NS))
    s = d[idx].sum(axis=1)
    diff_rows[n] = d
    print('%-16s %10.0f %10.0f %10.0f %7.1f%%'
          % (n, d.sum(), np.median(s), np.percentile(s, 5),
             100 * float((s > 0).mean())))

# ---------- 5. 池子内容与换手 ----------
print('\n[5] 池子规模与换手')
for n in NAMES[1:]:
    info = RS['pools'].get(n)
    if not info:
        continue
    sels = [set(x['sel']) for x in info]
    turns = []
    for i in range(1, len(sels)):
        a, b = sels[i - 1], sels[i]
        turns.append(len(b - a) / max(1, len(b)))
    print('  %-16s 调仓%d次  池规模 %s  平均换手 %.0f%%'
          % (n, len(sels), [len(s) for s in sels],
             100 * float(np.mean(turns)) if turns else 0.0))

# ---------- 6. 持仓天数桶 & 成本 ----------
print('\n[6] 持仓天数桶（净，元）与成本占比')
for n in NAMES:
    df = D[n]
    b = []
    for lo, hi in ((0, 3), (3, 10), (10, 31)):
        g = df[(df.hold_days >= lo) & (df.hold_days < hi)]
        b.append('%d-%d:%+.0f(%d)' % (lo, hi, g.pnl.sum() if len(g) else 0.0, len(g)))
    print('  %-16s %s  成本/毛利 %.1f%%'
          % (n, '  '.join(b),
             100 * float(df.cost.sum() / (df.pnl.sum() + df.cost.sum()))))
