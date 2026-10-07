# -*- coding: utf-8 -*-
"""滚动品种池筛选：参数网格（调仓频率 × 回看窗口 × 入选规则）——检验是「高原」还是「尖峰」。

上下文（事件 + 两个驱动）落盘 /tmp/roll_ctx.pkl，网格跑批只需秒级。
"""
import os
import sys
import time
import json
import pickle

sys.path.insert(0, '/app')
sys.path.insert(0, '/tmp')

import numpy as np
import pandas as pd

import run_param_scan_v3 as V
import run_roll_univ as R

OUT = '/tmp/roll_out'
CTX = '/tmp/roll_ctx.pkl'
os.makedirs(OUT, exist_ok=True)


def log(*a):
    print('[roll2]', *a, flush=True)


def quarterly_rbs(start, end):
    y0, y1 = int(str(start)[:4]), int(str(end)[:4])
    out = []
    for y in range(y0, y1 + 1):
        for md in ('01-01', '04-01', '07-01', '10-01'):
            d = np.datetime64('%d-%s' % (y, md))
            if start <= d <= end:
                out.append(d)
    return out


def main():
    V._isolate_cost_connections()
    t0 = time.time()
    if os.path.exists(CTX):
        ctx = pickle.load(open(CTX, 'rb'))
        log('ctx loaded (%.0fs)' % (time.time() - t0))
    else:
        engine = V.get_engine()
        universe, skipped = V.build_universe(engine)
        data = V.pickle.load(open(V.CACHE, 'rb'))
        data = {k: v for k, v in data.items() if k in set(universe)}
        universe = sorted(data)
        margin, msrc = V.load_margin_map(engine, universe)
        all_days = sorted({d for sd in data.values() for d in sd['days']})
        days_global = np.array(all_days, dtype='datetime64[D]')
        close_map = {sd['sym']: sd['close_map'] for sd in data.values()}
        ev_all = []
        for sym, sd in data.items():
            ev = V.gen_events(sd, R.PBASE, 2.0, False)
            for e in ev:
                e['_close_map'] = close_map[sym]
            ev_all.extend(ev)
        log('events=%d (%.0fs)' % (len(ev_all), time.time() - t0))
        ref = V.simulate(ev_all, days_global, R.PBASE, margin=margin,
                         max_products=R.MP, max_hold=R.HOLD)
        df_ref = pd.DataFrame(ref['trades'])
        drvA = R.make_driver(df_ref)
        solo = []
        for sym in universe:
            s1 = V.simulate([e for e in ev_all if e['sym'] == sym], days_global,
                            R.PBASE, margin=margin, cap=False,
                            max_products=9999, max_hold=R.HOLD)
            solo.extend(s1['trades'])
        df_solo = pd.DataFrame(solo)
        drvB = R.make_driver(df_solo)
        ctx = dict(universe=universe, margin=margin, days_global=days_global,
                   ev_all=ev_all, drvA=drvA, drvB=drvB,
                   ref_net=float(df_ref.pnl.sum()), ref_n=int(len(df_ref)),
                   solo_net=float(df_solo.pnl.sum()), solo_n=int(len(df_solo)))
        pickle.dump(ctx, open(CTX, 'wb'))
        log('ctx built (%.0fs) ref=%.0f solo=%.0f'
            % (time.time() - t0, ctx['ref_net'], ctx['solo_net']))

    universe = ctx['universe']
    margin = ctx['margin']
    days_global = ctx['days_global']
    ev_all = ctx['ev_all']
    days_oos = days_global[days_global >= R.CUT]
    ev_oos = [e for e in ev_all if e['d'] >= R.CUT]
    log('OOS days=%d events=%d' % (len(days_oos), len(ev_oos)))

    RB = {'1Y': R.annual_rbs(R.CUT, days_oos[-1]),
          '6M': R.semiannual_rbs(R.CUT, days_oos[-1]),
          '3M': quarterly_rbs(R.CUT, days_oos[-1])}
    WIN = {'W2': 730, 'W3': 1095, 'W5': 1825}
    RULE = {'POS': ('POS', None), 'T15': ('TOP', 15), 'T20': ('TOP', 20)}

    CFG = [('BASE', None)]
    for rn, rbs in RB.items():
        for wn, wd in WIN.items():
            for un, (rule, k) in RULE.items():
                CFG.append(('A_%s_%s_%s' % (rn, wn, un),
                            (ctx['drvA'], rbs, wd, rule, k)))
    log('configs=%d' % len(CFG))

    results = {}
    for name, spec in CFG:
        t1 = time.time()
        if spec is None:
            sel, info = None, None
        else:
            drv, rbs, wd, rule, k = spec
            pools, info = R.build_pools(drv, rbs, wd, rule, k=k, min_tr=5,
                                        universe=universe)
            sel = R.PoolSel(rbs, pools)
        sim = R.simulate_pool(ev_oos, days_oos, R.PBASE, margin=margin,
                              max_products=R.MP, max_hold=R.HOLD, pool_at=sel)
        df = pd.DataFrame(sim['trades'])
        if not len(df):
            log('%-18s 无交易' % name)
            continue
        net = float(df.pnl.sum())
        wins, losses = df[df.pnl > 0], df[df.pnl <= 0]
        pf = (float(wins.pnl.sum() / abs(losses.pnl.sum()))
              if len(losses) and losses.pnl.sum() != 0 else float('inf'))
        mdd, eq = R.equity_curve(df, days_oos)
        ps = [x['n_sel'] for x in info] if info else []
        m = dict(name=name, n=int(len(df)), net=round(net, 0), pf=round(pf, 3),
                 win=round(100 * len(wins) / len(df), 1),
                 cost=round(float(df.cost.sum()), 0),
                 per_trade=round(net / len(df), 0),
                 n_syms=int(df.symbol.nunique()),
                 peak_margin=sim['peak_margin'], mdd=round(mdd, 0),
                 suggest=round(sim['peak_margin'] + abs(mdd), 0),
                 filtered=sim['filtered'], blocked=sim['blocked'],
                 by_year={str(y): round(float(g.pnl.sum()), 0)
                          for y, g in df.groupby('eyear')},
                 pool_avg=(round(float(np.mean(ps)), 1) if ps else None),
                 top5=[dict(s=s, v=round(float(v), 0)) for s, v in
                       df.groupby('symbol').pnl.sum().sort_values(ascending=False).head(5).items()])
        results[name] = m
        df.to_csv(os.path.join(OUT, 'g_trades_%s.csv' % name),
                  index=False, encoding='utf-8-sig')
        log('%-18s n=%-5d net=%-10.0f pf=%.3f 池均%5s 峰值保%.0f MDD%.0f (%.0fs)'
            % (name, m['n'], m['net'], m['pf'], m['pool_avg'],
               m['peak_margin'], m['mdd'], time.time() - t1))

    with open(os.path.join(OUT, 'grid_results.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(ref_net=ctx['ref_net'], ref_n=ctx['ref_n'],
                       solo_net=ctx['solo_net'], oos_days=len(days_oos),
                       results=results), f,
                  ensure_ascii=False, indent=1, default=str)
    log('DONE %.0fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
