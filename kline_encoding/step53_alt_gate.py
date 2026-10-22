"""step53: 去掉 M5 会员门控 + 单品种可计算替代因子扫描
全市场 56 品种，分 IS(2020-2025)/OOS(2026)。
覆盖率 = 该配置总成交 / 去体制门(ALT_GATE='none')总成交。
已验证引擎 fusion_v5x_engine.SymbolEngine 逐笔一致。
"""
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


def run_cfg(cfg):
    tot = []
    tgt = {s: [] for s in TARGET if s in data}
    for s in data:
        tr = replay(s, data[s], cfg, costmap, tick, bm[s])
        tot.extend(tr)
        if s in tgt:
            tgt[s] = tr
    return tot, tgt


def split(tr):
    IS = [t for t in tr if t['xdt'].year <= 2025]
    OOS = [t for t in tr if t['xdt'].year == 2026]
    return IS, OOS


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


# 网格：gate, thr, N
GRID = [
    ('m5', None, None),     # 对照（含 M5）
    ('none', None, None),   # 去体制门（去 M5）
    ('emadist', 1.0, None), ('emadist', 1.5, None), ('emadist', 2.0, None),
    ('emadist', 2.5, None), ('emadist', 3.0, None),
    ('mom', 0.02, 20), ('mom', 0.03, 20), ('mom', 0.05, 20),
    ('mom', 0.03, 10), ('mom', 0.03, 40),
    ('adx', 20, None), ('adx', 25, None), ('adx', 30, None),
    ('dirpersist', 10, None), ('dirpersist', 15, None), ('dirpersist', 20, None),
    ('mom_or_emadist', 0.02, 20), ('mom_or_emadist', 0.03, 20), ('mom_or_emadist', 0.05, 20),
]

rows = []
N_none = None
for gi, (gate, thr, N) in enumerate(GRID):
    cfg = V5XConfig()
    cfg.ADD_MAX_LOTS = 2
    cfg.ALT_GATE = gate
    if thr is not None:
        cfg.ALT_THR = thr
    if N is not None:
        cfg.ALT_N = N
    tot, tgt = run_cfg(cfg)
    Ngate = len(tot)
    if gate == 'none':
        N_none = Ngate
    cov = (Ngate / N_none) if N_none else float('nan')
    IS, OOS = split(tot)
    mIS = metrics(IS); mOOS = metrics(OOS); mT = metrics(tot)
    eb = metrics(tgt.get('EB888', [])); eg = metrics(tgt.get('EG888', []))
    rows.append(dict(gate=gate, thr=thr, N=N, cov=round(cov, 3),
                     n=mT['n'], net=mT['net'], pf=mT['pf'], win=mT['win'], maxdd=mT['maxdd'],
                     nIS=mIS['n'], netIS=mIS['net'], pfIS=mIS['pf'],
                     nOOS=mOOS['n'], netOOS=mOOS['net'], pfOOS=mOOS['pf'],
                     EB_n=eb['n'], EB_net=eb['net'], EB_pf=eb['pf'],
                     EG_n=eg['n'], EG_net=eg['net'], EG_pf=eg['pf']))
    print(f"[{gi+1}/{len(GRID)}] {gate:14s} thr={str(thr):5s} N={str(N):4s} "
          f"cov={cov:.3f} 全{ mT['n']:5d}笔/{mT['net']:>11,.0f} PF{mT['pf']:.2f} "
          f"IS {mIS['net']:>11,.0f} OOS {mOOS['net']:>9,.0f}/{mOOS['pf']:.2f} "
          f"| EB {eb['net']:>8,.0f}/{eb['pf']:.2f} EG {eg['net']:>8,.0f}/{eg['pf']:.2f}", flush=True)

df = pd.DataFrame(rows)
df.to_csv('step53_alt_gate_result.csv', index=False)
print('\n=== 结果表 ===')
print(df.to_string(index=False, float_format=lambda x: f'{x:,.2f}'))
