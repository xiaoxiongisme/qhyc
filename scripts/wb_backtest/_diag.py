import pickle, numpy as np, sys
sys.path.insert(0, '/app'); sys.path.insert(0, '/tmp')
import run_four as F
data = pickle.load(open('/tmp/stop_cali_cache.pkl', 'rb'))
is_res = pickle.load(open('/tmp/four_is.pkl', 'rb'))
cut = np.datetime64('2021-01-01')
sets = {}
for sname, sc in F.STRATS.items():
    p = is_res[sname]['best_p']
    gen = sc['gen']
    traded = set()
    for sym, sd in data.items():
        ev = gen(sd, p)
        ev = [e for e in ev if e['d'] < cut]
        if len(ev) >= 1:
            traded.add(sym)
    sets[sname] = traded
    print('%-7s best=%s  IS交易品种数=%d' % (sname, p, len(traded)))
print('--- 交集(四策略都交易的品种数) ---')
common = set(data.keys())
for s, t in sets.items():
    common &= t
print('四策略共同交易品种:', len(common))
print('至少1策略交易:', len(set().union(*sets.values())))
print('零交易品种(任何策略都不交易):', len(set(data.keys()) - set().union(*sets.values())))
