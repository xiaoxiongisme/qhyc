# -*- coding: utf-8 -*-
"""推荐配置细节：逐年、品种、资金量、池子内容。"""
import os
import json
import numpy as np
import pandas as pd

OUT = '/tmp/roll_out'
GR = json.load(open(os.path.join(OUT, 'grid_results.json'), encoding='utf-8'))
RR = json.load(open(os.path.join(OUT, 'rand_results.json'), encoding='utf-8'))
R1 = json.load(open(os.path.join(OUT, 'results.json'), encoding='utf-8'))
RES = GR['results']


def df_of(n):
    return pd.read_csv(os.path.join(OUT, 'g_trades_%s.csv' % n),
                       encoding='utf-8-sig')


print('=' * 96)
print('零假设对照（随机池，每期随机挑同样多的合格品种，300 次）')
print('=' * 96)
print('%-18s %11s %11s %11s %11s %9s' % ('口径', '随机中位', '随机5%', '随机95%', '定向筛选', '百分位'))
for k, v in RR.items():
    if not k.startswith('RAND'):
        continue
    print('%-18s %11.0f %11.0f %11.0f %11.0f %8.1f%%'
          % (k[5:], v['median'], v['p5'], v['p95'], v['real'], v['pct_rank']))
print('%-18s %11s %11s %11s %11.0f %9s'
      % ('基线(不筛65)', '-', '-', '-', RES['BASE']['net'], '-'))
print('\n反向对照（专挑过去净<=0 的品种）：')
print('  NEG_1Y_W3  net=%.0f  (同口径 POS = %.0f)'
      % (RR['NEG_1Y_W3']['net'], RES['A_1Y_W3_POS']['net']))
print('  NEG_6M_W5  net=%.0f  (同口径 POS = %.0f)'
      % (RR['NEG_6M_W5']['net'], RES['A_6M_W5_POS']['net']))

REC = ['A_1Y_W5_T15', 'A_6M_W5_T15', 'A_3M_W5_T15']
print('\n' + '=' * 96)
print('推荐配置（5 年回看 + Top15；调仓频率是高原内自由参数）')
print('=' * 96)
print('%-14s %7s %11s %7s %8s %12s %11s %11s %9s'
      % ('配置', '笔数', '净', 'PF', '单笔', '峰值保证金', 'MDD', '建议资金', '净/|MDD|'))
for n in REC + ['BASE']:
    m = RES[n]
    print('%-14s %7d %11.0f %7.3f %8.0f %12.0f %11.0f %11.0f %9.2f'
          % (n, m['n'], m['net'], m['pf'], m['per_trade'], m['peak_margin'],
             m['mdd'], m['peak_margin'] + abs(m['mdd']),
             m['net'] / abs(m['mdd'])))

print('\n[逐年净（元）]')
years = ['2021', '2022', '2023', '2024', '2025', '2026']
print('%-14s' % '配置' + ''.join('%11s' % y for y in years))
for n in REC + ['BASE']:
    m = RES[n]
    print('%-14s' % n + ''.join('%11.0f' % m['by_year'].get(y, 0) for y in years))
print('%-14s' % '推荐−基线' + ''.join(
    '%+11.0f' % (RES['A_6M_W5_T15']['by_year'].get(y, 0) - RES['BASE']['by_year'].get(y, 0))
    for y in years))

print('\n[池子内容]（A_6M_W5_T15，半年调仓 12 期）')
pools = R1['pools']
info = json.load(open(os.path.join(OUT, 'results.json'), encoding='utf-8'))['pools']
# 网格没存 sel 明细，用 v1 的 A_1Y_W5_POS 作示意 + 从 trades 反推实际参与品种
d = df_of('A_6M_W5_T15')
b = df_of('BASE')
print('  实际产生交易的品种数：筛选 %d / 不筛 %d' % (d.symbol.nunique(), b.symbol.nunique()))
print('  筛选后 OOS 净收益 TOP8：')
for s, v in d.groupby('symbol').pnl.sum().sort_values(ascending=False).head(8).items():
    vb = float(b[b.symbol == s].pnl.sum())
    print('    %-8s 筛选 %10.0f   不筛 %10.0f   差 %+10.0f' % (s, v, vb, v - vb))
print('  被筛掉（不在任何一期池中）而基线有交易的品种：')
sel_syms = set(d.symbol)
gone = sorted(set(b.symbol) - sel_syms)
print('    %d 个：%s' % (len(gone), gone))
print('    这些品种在基线中的合计净收益：%.0f'
      % float(b[b.symbol.isin(gone)].pnl.sum()))

print('\n[成本与持仓结构]')
for n in REC + ['BASE']:
    x = df_of(n)
    cost = float(x.cost.sum())
    print('  %-14s 成本 %.0f  占毛利 %.1f%%  0-3天 %+.0f(%d)  3-10天 %+.0f(%d)  10-31天 %+.0f(%d)'
          % (n, cost, 100 * cost / (float(x.pnl.sum()) + cost),
             x[x.hold_days < 3].pnl.sum(), (x.hold_days < 3).sum(),
             x[(x.hold_days >= 3) & (x.hold_days < 10)].pnl.sum(),
             ((x.hold_days >= 3) & (x.hold_days < 10)).sum(),
             x[x.hold_days >= 10].pnl.sum(), (x.hold_days >= 10).sum()))
