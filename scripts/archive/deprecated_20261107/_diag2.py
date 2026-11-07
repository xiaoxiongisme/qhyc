import pickle, numpy as np, sys
sys.path.insert(0, '/app'); sys.path.insert(0, '/tmp')
import run_four as F
data = pickle.load(open('/tmp/stop_cali_cache.pkl', 'rb'))
is_res = pickle.load(open('/tmp/four_is.pkl', 'rb'))
cut = np.datetime64('2021-01-01')
# recompute traded sets
sets = {}
for sname, sc in F.STRATS.items():
    p = is_res[sname]['best_p']; gen = sc['gen']; traded = set()
    for sym, sd in data.items():
        ev = [e for e in gen(sd, p) if e['d'] < cut]
        if ev: traded.add(sym)
    sets[sname] = traded
common = set(data.keys())
for t in sets.values(): common &= t
excluded = sorted(set(data.keys()) - common)
print('排除的品种(%d):' % len(excluded), excluded)
print()
print('%-8s %12s %14s %10s %10s' % ('sym', 'first_day', 'sym_cut', 'n_bars', 'n_after_cut'))
for sym in excluded[:35]:
    sd = data[sym]
    fc = sd['c']
    nanc = int(np.isnan(fc).sum())
    nafter = int(sum(1 for d in sd['days'] if d >= sd['sym_cut']))
    print('%-8s %12s %14s %10d %10d (nan_c=%d)' % (
        sym, str(sd['days'][0]), str(sd['sym_cut']), len(fc), nafter, nanc))
