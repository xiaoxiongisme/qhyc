# -*- coding: utf-8 -*-
"""定位 R2_CONF 空头为 0 的根因。"""
import sys
import pickle
import numpy as np
sys.path.insert(0, '/tmp')
sys.path.insert(0, '/app')

import run_param_scan_v3 as V
import run_rule123 as R

data = pickle.load(open('/tmp/stop_cali_cache.pkl', 'rb'))
P = V.make_P(stop_scale=2.0)

tot_in = {1: 0, -1: 0}
tot_ok = {1: 0, -1: 0}
for sym in ['AU888', 'RB888', 'CU888', 'A888', 'JM888']:
    sd = data[sym]
    fu = V.gen_events(sd, P, 2.0, False)
    idx = {t: i for i, t in enumerate(sd['dts'])}
    sig = R.rule_signals(sd, pivot_k=5, use_htf=True)
    ins = {1: 0, -1: 0}
    ok = {1: 0, -1: 0}
    miss_dt = 0
    for e in fu:
        if e['kind'] != 'ENTRY':
            continue
        ins[e['dir']] += 1
        bi = idx.get(e['dt'])
        if bi is None:
            miss_dt += 1
            continue
        if np.any(sig[max(0, bi - 24):bi + 1] == e['dir']):
            ok[e['dir']] += 1
    print('%-8s 入场 多%4d 空%4d → 通过过滤 多%4d 空%4d  dt缺失%3d  htf正占比%.1f%%'
          % (sym, ins[1], ins[-1], ok[1], ok[-1], miss_dt,
             100.0 * (sd['htf_dir'] > 0).mean()))
    tot_in[1] += ins[1]
    tot_in[-1] += ins[-1]
    tot_ok[1] += ok[1]
    tot_ok[-1] += ok[-1]
print()
print('合计 入场 多%d 空%d → 通过 多%d 空%d'
      % (tot_in[1], tot_in[-1], tot_ok[1], tot_ok[-1]))

# 再看 R1 生成器本身的持仓方向分布
print()
print('=== R1_ONLY 发出来的 ENTRY 方向分布（5 个品种）===')
for sym in ['AU888', 'RB888', 'CU888', 'A888', 'JM888']:
    sd = data[sym]
    ev = R.gen_events_r1(sd)
    ent = [e for e in ev if e['kind'] == 'ENTRY']
    print('  %-8s ENTRY 多%4d 空%4d' % (sym, sum(1 for e in ent if e['dir'] == 1),
                                        sum(1 for e in ent if e['dir'] == -1)))
