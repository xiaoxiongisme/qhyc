import pickle, numpy as np, sys
sys.path.insert(0, '/app'); sys.path.insert(0, '/tmp')
import run_four as F
data = pickle.load(open('/tmp/stop_cali_cache.pkl', 'rb'))
is_res = pickle.load(open('/tmp/four_is.pkl', 'rb'))
cut = np.datetime64('2021-01-01')
for sym in ('NI888', 'SN888', 'A888'):
    sd = data[sym]
    print('===', sym)
    for sname, sc in F.STRATS.items():
        p = is_res[sname]['best_p']; ev = [e for e in sc['gen'](sd, p) if e['d'] < cut]
        print('  %-7s IS events=%d' % (sname, len(ev)))
