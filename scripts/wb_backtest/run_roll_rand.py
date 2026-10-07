# -*- coding: utf-8 -*-
"""零假设对照：随机池 / 反向池。

若「每期随机挑同样数量的品种」也能得到相近的增益，则滚动筛选的价值 = 0，
真正的成因只是「品种数变少」。这是必须排除的零假设。
另加 W5/T15 口径下的反向对照（选净<=0 的品种）。
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
N_RAND = 300


def log(*a):
    print('[rand]', *a, flush=True)


def simulate_net(ev_oos, days_oos, margin, sel, PBASE=R.PBASE):
    sim = R.simulate_pool(ev_oos, days_oos, PBASE, margin=margin,
                          max_products=R.MP, max_hold=R.HOLD, pool_at=sel)
    return float(pd.DataFrame(sim['trades']).pnl.sum()), len(sim['trades'])


def main():
    t0 = time.time()
    ctx = pickle.load(open(CTX, 'rb'))
    universe = ctx['universe']
    margin = ctx['margin']
    days_global = ctx['days_global']
    ev_all = ctx['ev_all']
    days_oos = days_global[days_global >= R.CUT]
    ev_oos = [e for e in ev_all if e['d'] >= R.CUT]
    log('OOS days=%d events=%d' % (len(days_oos), len(ev_oos)))

    RB = {'1Y': R.annual_rbs(R.CUT, days_oos[-1]),
          '6M': R.semiannual_rbs(R.CUT, days_oos[-1])}
    WIN = {'W3': 1095, 'W5': 1825}

    out = {}

    # ---------- 反向对照（W5/T15 与 W3/POS 两个口径） ----------
    for tag, (rn, wn, rule, k) in {
            'NEG_1Y_W3': ('1Y', 'W3', 'NEG', None),
            'NEG_6M_W5': ('6M', 'W5', 'NEG', None)}.items():
        pools, info = R.build_pools(ctx['drvA'], RB[rn], WIN[wn], rule, k=k,
                                    min_tr=5, universe=universe)
        net, n = simulate_net(ev_oos, days_oos, margin, R.PoolSel(RB[rn], pools))
        out[tag] = dict(net=round(net, 0), n=n,
                        pool_avg=round(float(np.mean([x['n_sel'] for x in info])), 1))
        log('%-14s n=%-5d net=%-11.0f 池均%.1f' % (tag, n, net, out[tag]['pool_avg']))

    # ---------- 随机池零假设 ----------
    rng = np.random.default_rng(20261007)
    for rn in ('1Y', '6M'):
        for wn in ('W3', 'W5'):
            rbs = RB[rn]
            wd = WIN[wn]
            # 每期「合格集合」（有 >=5 笔交易的品种）——与筛选共用同一可入选域
            elig, sizes = [], []
            for rb in rbs:
                lo = rb - np.timedelta64(wd, 'D')
                e = [s for s in universe
                     if R.window_net(ctx['drvA'], s, lo, rb)[1] >= 5]
                elig.append(np.array(sorted(e)))
            # 真实筛选（T15）的每期池规模
            pools, info = R.build_pools(ctx['drvA'], rbs, wd, 'TOP', k=15,
                                        min_tr=5, universe=universe)
            sizes = [len(p) for p in pools]
            nets_real, _ = simulate_net(ev_oos, days_oos, margin,
                                        R.PoolSel(rbs, pools))
            out['REAL_%s_%s_T15' % (rn, wn)] = dict(net=round(nets_real, 0))
            # 随机池：每期从合格集合随机抽与真实池同样多的品种
            draws = []
            for b in range(N_RAND):
                rp = []
                for i, e in enumerate(elig):
                    kk = min(sizes[i], len(e))
                    rp.append(set(rng.choice(e, size=kk, replace=False).tolist()))
                net, _ = simulate_net(ev_oos, days_oos, margin, R.PoolSel(rbs, rp))
                draws.append(net)
            draws = np.array(draws)
            obs = nets_real
            out['RAND_%s_%s_T15' % (rn, wn)] = dict(
                median=round(float(np.median(draws)), 0),
                mean=round(float(draws.mean()), 0),
                p5=round(float(np.percentile(draws, 5)), 0),
                p95=round(float(np.percentile(draws, 95)), 0),
                real=round(obs, 0),
                pct_rank=round(100 * float((draws < obs).mean()), 1))
            log('%-18s 随机池中位%.0f [5%%=%.0f, 95%%=%.0f]  真实%.0f  百分位%.1f%%  (%.0fs)'
                % ('RAND_%s_%s_T15' % (rn, wn), np.median(draws),
                   np.percentile(draws, 5), np.percentile(draws, 95), obs,
                   100 * float((draws < obs).mean()), time.time() - t0))

    with open(os.path.join(OUT, 'rand_results.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    log('DONE %.0fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
