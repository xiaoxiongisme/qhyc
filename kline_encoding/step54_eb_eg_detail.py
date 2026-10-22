"""step54: EB/EG 单品种在 dirpersist/emadist 替代门下的 IS/OOS 拆分 + 回撤。
用于给用户"去掉 M5 后单品种怎么配"的精确参数。"""
import numpy as np, pandas as pd
import warnings; warnings.filterwarnings('ignore')
import step46_prod_bt as s46
from step49_winrate import replay
from fusion_v5x_engine import V5XConfig, CLUSTER

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
EXCL = s46.EXCLUDE
syms = sorted(s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in EXCL)
data = {s: b for s in syms if (b := s46.build_sym(s, ba, raw, dd, var)) is not None}
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)
bm = {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}
TARGET = ('EB888', 'EG888')


def metrics(tr):
    if not tr:
        return dict(n=0, net=0.0, pf=float('nan'), win=float('nan'), maxdd=0.0)
    n = len(tr); net = sum(t['net'] for t in tr)
    w = [t for t in tr if t['net'] > 0]
    gl = sum(-t['net'] for t in tr if t['net'] < 0)
    gp = sum(t['net'] for t in w)
    pf = gp / gl if gl > 0 else float('nan')
    win = len(w) / n
    trs = sorted(tr, key=lambda t: t['xdt'])
    eq = 0.0; peak = 0.0; mdd = 0.0
    for t in trs:
        eq += t['net']; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    return dict(n=n, net=round(net, 1), pf=round(pf, 3), win=round(win, 4), maxdd=round(mdd, 1))


def split(tr):
    IS = [t for t in tr if t['xdt'].year <= 2025]
    OOS = [t for t in tr if t['xdt'].year == 2026]
    return IS, OOS


GRID = [('m5', None), ('dirpersist', 10), ('dirpersist', 15), ('dirpersist', 20),
        ('emadist', 1.0), ('emadist', 1.5), ('emadist', 2.0)]
rows = []
for s in TARGET:
    name = '苯乙烯' if s == 'EB888' else '乙二醇'
    for gate, thr in GRID:
        cfg = V5XConfig(); cfg.ADD_MAX_LOTS = 2; cfg.ALT_GATE = gate
        if thr is not None:
            cfg.ALT_THR = thr
        tr = replay(s, data[s], cfg, costmap, tick, bm[s])
        mT, mIS, mOOS = metrics(tr), *[metrics(x) for x in split(tr)]
        yrs = (max(t['xdt'] for t in tr) - min(t['xdt'] for t in tr)).days / 365.25 if tr else 0
        rows.append(dict(sym=s, name=name, gate=gate, thr=thr,
                        n=mT['n'], net=mT['net'], pf=mT['pf'], win=mT['win'], maxdd=mT['maxdd'],
                        per_yr=round(mT['n'] / yrs, 2) if yrs else 0,
                        nIS=mIS['n'], netIS=mIS['net'], pfIS=mIS['pf'],
                        nOOS=mOOS['n'], netOOS=mOOS['net'], pfOOS=mOOS['pf']))
        print(f"{name:4s} {gate:10s} thr={str(thr):4s} 全{mT['n']:3d}笔/{mT['net']:>9,.0f}/PF{mT['pf']:.2f}"
              f"/MDD{mT['maxdd']:>8,.0f}  IS{mIS['n']:3d}/{mIS['net']:>9,.0f}/PF{mIS['pf']:.2f}"
              f"  OOS{mOOS['n']:3d}/{mOOS['net']:>8,.0f}/PF{mOOS['pf']:.2f}", flush=True)
pd.DataFrame(rows).to_csv('step54_eb_eg_detail.csv', index=False)
print('saved step54_eb_eg_detail.csv')
