# -*- coding: utf-8 -*-
"""step57b：全市场确认 dirpersist 不是退化替代（对照 M5 会员）。
复用已验证引擎，跑全 56 品种。"""
import json
import numpy as np
import pandas as pd
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CLUSTER
from step49_winrate import replay

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
EXCL = s46.EXCLUDE
syms = sorted(s for s in ba.symbol.unique()
              if s[:-3] in costmap and s[:-3] not in EXCL)
data = {}
for s in syms:
    b = s46.build_sym(s, ba, raw, dd, var)
    if b is not None:
        data[s] = b
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)
bm = {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}
print(f'loaded {len(data)} syms', flush=True)


def summ(trades):
    if not trades:
        return dict(n=0, net=0.0, pf=np.nan, win=np.nan, ho_net=0.0, ho_n=0)
    df = pd.DataFrame(trades)
    w = df[df.net > 0]; l = df[df.net < 0]
    gp = float(w.net.sum()); gl = float(-l.net.sum()) if len(l) else 0.0
    ho = df[df.year >= 2026]
    return dict(n=len(df), net=float(df.net.sum()),
                pf=float(gp / gl) if gl > 0 else np.nan,
                win=float(len(w) / len(df)),
                ho_net=float(ho.net.sum()), ho_n=int(len(ho)))


configs = {
    'M5_member': dict(ALT_GATE='m5', M5_ON=True, M5_Z_THR=1.0),
    'dirpersist_10': dict(ALT_GATE='dirpersist', ALT_THR=10.0),
    'dirpersist_5': dict(ALT_GATE='dirpersist', ALT_THR=5.0),
}
out = {}
for cname, ov in configs.items():
    cfg = V5XConfig(**ov)
    allt = []
    for s in data:
        allt += replay(s, data[s], cfg, costmap, tick, bm[s])
    m = summ(allt)
    out[cname] = m
    print(cname, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()}, flush=True)

json.dump(out, open('step57b_wholemarket.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
print('saved step57b_wholemarket.json')
