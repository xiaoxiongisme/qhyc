import pickle, numpy as np, sys
sys.path.insert(0, '/app'); sys.path.insert(0, '/tmp')
import run_four as F
data = pickle.load(open('/tmp/stop_cali_cache.pkl', 'rb'))
cut = np.datetime64('2021-01-01')
for sym in ('NI888', 'SN888'):
    sd = data[sym]
    print('===', sym, 'sym_cut', sd['sym_cut'], 'n', len(sd['c']))
    # check fields
    for f in ('c', 'h', 'l', 'o', 'raw_close', 'htf_dir', 'atr', 'ma20', 'adx'):
        arr = np.asarray(sd[f], float)
        print('  %-10s nan=%d min=%s max=%s' % (f, int(np.isnan(arr).sum()),
              '%.2f' % np.nanmin(arr) if np.isfinite(np.nanmin(arr)) else 'nan',
              '%.2f' % np.nanmax(arr) if np.isfinite(np.nanmax(arr)) else 'nan'))
    # run each generator, count IS events
    is_res = pickle.load(open('/tmp/four_is.pkl', 'rb'))
    for sname, sc in F.STRATS.items():
        p = is_res[sname]['best_p']; ev = [e for e in sc['gen'](sd, p) if e['d'] < cut]
        print('  %-7s IS events=%d' % (sname, len(ev)))
