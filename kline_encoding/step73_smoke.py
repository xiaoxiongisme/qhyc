# -*- coding: utf-8 -*-
"""smoke: validate import + global-patch + one V5X baseline run."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time, json
import step46_prod_bt as S

ba, raw, dd, var, cost, fac = S.load_all()
costmap = S.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
syms = sorted(s for s in ba.symbol.unique()
              if s[:-3] in costmap and s[:-3] not in S.EXCLUDE)
data = {}
for s in syms:
    try:
        b = S.build_sym(s, ba, raw, dd, var)
        if b is not None:
            data[s] = b
    except Exception as e:
        print('skip', s, e)
data = S.attach_member_z(data, fac)
shock = S.cluster_shock_days(data)
print('loaded', len(data), 'syms', flush=True)

def cfg_for(sym, scope):
    vc = sym[:-3]; cl = S.CLUSTER.get(vc)
    cfg = {'cost': costmap[vc], 'tick': tick.get(vc, 1.0),
           'blocked_tds': shock.get(cl, set()) if cl else set()}
    if scope and cl in scope:
        cfg['entry_mode'] = 'pullback'
    return cfg

# patch baseline
for k, v in dict(SL_ATR=2.0, TRAIL_ATR=2.0, BE_R=0.5, COOLDOWN=3,
                 ADD_MAX_LOTS=2, ADD_THR=1.0).items():
    setattr(S, k, v)

t0 = time.time()
tb = {s: S.run_symbol(s, data[s], cfg_for(s, S.P4_SECTORS), {'A1S','B3','M1a_pb','M5'})
      for s in data}
trades = [t for ts in tb.values() for t in ts]
m = S.metrics(trades, 'V5X')
print('secs', round(time.time()-t0, 2), flush=True)
print(json.dumps({k: (round(x,3) if isinstance(x,float) else x)
                  for k,x in m.items() if k in ['n','win','pf','net_yuan','mdd_yuan','mar']},
                 ensure_ascii=False, default=str), flush=True)
print('train', json.dumps(m.get('train'), ensure_ascii=False, default=str), flush=True)
print('holdout', json.dumps(m.get('holdout'), ensure_ascii=False, default=str), flush=True)
