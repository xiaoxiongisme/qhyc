# -*- coding: utf-8 -*-
"""S1b：资金分层组合（两策略各独立账户 cap=20，各半资金/全资金两口径）
从 T0_turtle_sel / F0_fusion_sel 的 OOS 明细合成，不重跑模拟。
"""
import os
import json
import numpy as np
import pandas as pd

OUT = '/tmp/stack_out'
res = json.load(open(os.path.join(OUT, 'stack_results.json')))


def eq_stats(df, label):
    g = df.groupby('exit_date')['pnl'].sum().sort_index()
    cum = g.cumsum()
    dd = (cum - cum.cummax()).min()
    out = dict(net=round(float(g.sum()), 0), mdd=round(float(dd), 0),
               n=len(df), days=len(g))
    return g, out


d_t = pd.read_csv(os.path.join(OUT, 'oos_T0_turtle_sel.csv'))
d_f = pd.read_csv(os.path.join(OUT, 'oos_F0_fusion_sel.csv'))
g_t, s_t = eq_stats(d_t, 'turtle')
g_f, s_f = eq_stats(d_f, 'fusion')
j = pd.concat([g_t, g_f], axis=1, keys=['t', 'f']).fillna(0.0).sort_index()
comb = j['t'] + j['f']
cum = comb.cumsum()
mdd_comb = float((cum - cum.cummax()).min())

cap_t = res['variants']['T0_turtle']['sel']['suggest_capital']
cap_f = res['variants']['F0_fusion']['sel']['suggest_capital']
print('turtle sel: net=%.0f mdd=%.0f n=%d cap=%.0f eff=%.3f'
      % (s_t['net'], s_t['mdd'], s_t['n'], cap_t, s_t['net'] / cap_t))
print('fusion sel: net=%.0f mdd=%.0f n=%d cap=%.0f eff=%.3f'
      % (s_f['net'], s_f['mdd'], s_f['n'], cap_f, s_f['net'] / cap_f))
print('separate-account combo: net=%.0f mdd=%.0f cap=%.0f eff=%.3f days=%d'
      % (comb.sum(), mdd_comb, cap_t + cap_f, comb.sum() / (cap_t + cap_f), len(j)))
r = float(np.corrcoef(j['t'], j['f'])[0, 1])
print('corr=%.3f  vol_t=%.0f vol_f=%.0f' % (
    r, j['t'].std(), j['f'].std()))

# 重叠度：两策略 sel 品种重合 & 同向持仓重叠近似（同日同品种都有交易）
st = set(res['sel_turtle']); sf = set(res['sel_fusion'])
print('sel overlap: t=%d f=%d both=%d union=%d'
      % (len(st), len(sf), len(st & sf), len(st | sf)))
k_t = set(zip(d_t.symbol, d_t.entry_date, d_t.side))
k_f = set(zip(d_f.symbol, d_f.entry_date, d_f.side))
print('same-day same-sym trade overlap: t=%d f=%d both=%d'
      % (len(k_t), len(k_f), len(k_t & k_f)))

json.dump(dict(turtle=s_t, fusion=s_f, cap_t=cap_t, cap_f=cap_f,
               combo_net=round(float(comb.sum()), 0),
               combo_mdd=round(mdd_comb, 0),
               corr=round(r, 3),
               sel_overlap=len(st & sf), trade_overlap=len(k_t & k_f)),
          open(os.path.join(OUT, 's1b_separate.json'), 'w'),
          ensure_ascii=False, indent=1)
print('saved s1b_separate.json')
