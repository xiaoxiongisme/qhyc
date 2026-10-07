# -*- coding: utf-8 -*-
"""半样本同号检验（仅 swing / turtle）。

把 IS(2015-2020) 拆成 H1=2015-2018、H2=2019-2020 两个不重叠子窗，
对每策略在三个训练窗（H1 / H2 / FULL）分别选最优参数 + 选品（IS净>0且≥5笔），
然后：
  - 应用到 OOS(2021-2026) 纯样本外；
  - 交叉验证：H1 配置应用到 H2，H2 配置应用到 H1（IS 内部留一）；
  - 报告选品集合 Jaccard 重叠。

判决：H1 配置与 H2 配置是否都在 OOS 同号为正 —— 若都正，说明策略不依赖单一子期。
"""
import os
import sys
import time
import pickle
import json
import random

sys.path.insert(0, '/app')
sys.path.insert(0, '/tmp')

import numpy as np
import pandas as pd

import run_four as F
import run_param_scan_v3 as V

OUT = '/tmp/halffit_out'
os.makedirs(OUT, exist_ok=True)
CUT = np.datetime64('2021-01-01')
H1 = (np.datetime64('2015-01-01'), np.datetime64('2019-01-01'))
H2 = (np.datetime64('2019-01-01'), np.datetime64('2021-01-01'))
FULL = (np.datetime64('2015-01-01'), np.datetime64('2021-01-01'))
SMOKE = '--smoke' in sys.argv
WINDOWS = [('H1', H1), ('H2', H2), ('FULL', FULL)]
STRATS = ['swing', 'turtle']


def is_scan(sname, data, days_global, margin, lo, hi):
    sc = F.STRATS[sname]
    gen = sc['gen']; grid = sc['grid']; mh = sc['max_hold']
    best = None
    for p in grid:
        per_prod = {}
        for sym, sd in data.items():
            ev = gen(sd, p)
            ev = [e for e in ev if lo <= e['d'] < hi]
            if not ev:
                continue
            sim = F.simulate(ev, days_global[(days_global >= lo) & (days_global < hi)],
                            margin=margin, cap=False, max_products=9999,
                            max_hold=mh, add_max_lots=sc['add_max'])
            net = float(pd.DataFrame(sim['trades']).pnl.sum()) if sim['trades'] else 0.0
            ntr = len(sim['trades'])
            per_prod[sym] = (net, ntr)
        total = sum(v[0] for v in per_prod.values())
        if best is None or total > best['total']:
            best = dict(p=p, total=round(total, 0), per_prod=per_prod,
                        n_prod=len(per_prod))
    sel = sorted([s for s, (net, ntr) in best['per_prod'].items()
                  if net > 0 and ntr >= 5])
    return best, sel


def apply_window(sname, p, sel, data, days_global, margin, lo, hi):
    sc = F.STRATS[sname]
    ev = []
    for sym, sd in data.items():
        if sym not in sel:
            continue
        for e in sc['gen'](sd, p):
            e['_close_map'] = sd['close_map']
            if lo <= e['d'] < hi:
                ev.append(e)
    sim = F.simulate(ev, days_global[(days_global >= lo) & (days_global < hi)],
                     margin=margin, cap=True, max_products=sc['cap'],
                     max_hold=sc['max_hold'], add_max_lots=sc['add_max'])
    m = F.summarize(sim['trades'], sname, sc['max_hold'])
    m['peak_margin'] = sim['peak_margin']
    m['suggest_capital'] = round(sim['peak_margin'] + abs(m['mdd']), 0)
    return m


def oos_apply(sname, p, sel, data, days_global, margin):
    return apply_window(sname, p, sel, data, days_global, margin, CUT,
                        np.datetime64('2100-01-01'))


def load_data():
    V._isolate_cost_connections()
    engine = V.get_engine()
    universe, skipped = V.build_universe(engine)
    if os.path.exists(V.CACHE):
        with open(V.CACHE, 'rb') as f:
            data = pickle.load(f)
        if set(data) != set(universe):
            data = None
    else:
        data = None
    if data is None:
        data = {}
        for sym in universe:
            sd = V.load_symbol(sym, engine)
            if sd is not None:
                data[sym] = sd
        with open(V.CACHE, 'wb') as f:
            pickle.dump(data, f)
    margin, _ = V.load_margin_map(engine, universe)
    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    return data, days_global, margin, universe


def main():
    t0 = time.time()
    data, days_global, margin, universe = load_data()
    if SMOKE:
        keep = set(universe[:8])
        data = {k: v for k, v in data.items() if k in keep}
    print('data=%d days=%d OOS_days=%d' % (
        len(data), len(days_global), int((days_global >= CUT).sum())), flush=True)

    res = {}
    for sname in STRATS:
        print('\n==== %s ====' % sname, flush=True)
        cfgs = {}
        for wname, (lo, hi) in WINDOWS:
            best, sel = is_scan(sname, data, days_global, margin, lo, hi)
            cfgs[wname] = dict(p=best['p'], total=best['total'],
                               n_sel=len(sel), sel=set(sel))
            print('  [%s] train_total=%.0f n_sel=%d p=%s (%.0fs)' % (
                wname, best['total'], len(sel), best['p'], time.time() - t0), flush=True)
        # OOS 应用
        oos = {}
        for wname in ('H1', 'H2', 'FULL'):
            m = oos_apply(sname, cfgs[wname]['p'], cfgs[wname]['sel'],
                          data, days_global, margin)
            oos[wname] = m
            print('  [%s->OOS] net=%.0f pf=%.3f win=%.1f%% n=%d MDD=%.0f 建议%.0f' % (
                wname, m['net'], m['pf'], m['win'], m['n'], m['mdd'],
                m['suggest_capital']), flush=True)
        # 交叉验证（IS 内部留一）
        cv = {}
        cv['H1_on_H2'] = apply_window(sname, cfgs['H1']['p'], cfgs['H1']['sel'],
                                      data, days_global, margin, H2[0], H2[1])
        cv['H2_on_H1'] = apply_window(sname, cfgs['H2']['p'], cfgs['H2']['sel'],
                                      data, days_global, margin, H1[0], H1[1])
        # 选品重叠
        sH1, sH2, sF = cfgs['H1']['sel'], cfgs['H2']['sel'], cfgs['FULL']['sel']
        jac12 = len(sH1 & sH2) / len(sH1 | sH2) if (sH1 | sH2) else 0.0
        jac1f = len(sH1 & sF) / len(sH1 | sF) if (sH1 | sF) else 0.0
        jac2f = len(sH2 & sF) / len(sH2 | sF) if (sH2 | sF) else 0.0
        res[sname] = dict(cfgs=cfgs, oos={k: {kk: m[kk] for kk in
                              ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                               'suggest_capital', 'n_syms', 'by_year')} for k, m in oos.items()},
                          cv={k: {kk: m[kk] for kk in ('n', 'net', 'pf', 'win', 'mdd')}
                              for k, m in cv.items()},
                          overlap=dict(H1=len(sH1), H2=len(sH2), FULL=len(sF),
                                       jac_H1H2=round(jac12, 3),
                                       jac_H1FULL=round(jac1f, 3),
                                       jac_H2FULL=round(jac2f, 3),
                                       H1_and_H2_sel=sorted(sH1 & sH2)))
        # 存交叉窗口交易明细（H1 配置应用到 H2）供核对
        print('  [cv] H1_on_H2 net=%.0f | H2_on_H1 net=%.0f' % (
            cv['H1_on_H2']['net'], cv['H2_on_H1']['net']), flush=True)
        print('  [overlap] H1=%d H2=%d FULL=%d jac(H1,H2)=%.3f jac(H1,FULL)=%.3f jac(H2,FULL)=%.3f'
              % (len(sH1), len(sH2), len(sF), jac12, jac1f, jac2f), flush=True)

    with open(os.path.join(OUT, 'halffit.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print('\nDONE %.0fs' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
