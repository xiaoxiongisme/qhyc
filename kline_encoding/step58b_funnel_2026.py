# -*- coding: utf-8 -*-
"""EB/EG 2026（含 9-10 月）信号漏斗诊断：raw → P2(高波日) → M1a(MACD) → 成交。
输出 step58_funnel.json（每品种每月漏斗 + 高波日占比）。"""
import json
import numpy as np
import pandas as pd
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CostCfg, DailyCtx, SymbolEngine, CLUSTER

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
data = {}
for s in ['EB888', 'EG888']:
    b = s46.build_sym(s, ba, raw, dd, var)
    if b is not None:
        data[s] = b
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)
bm = {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}


def run_diagnose(sym):
    b = data[sym]; vc = sym[:-3]
    fee_row, slip, mult = costmap[vc]
    cfg = V5XConfig(); cfg.DIAGNOSE = True; cfg.ALT_GATE = 'none'
    eng = SymbolEngine(sym, cfg, CostCfg(fee_row, slip, mult, tick.get(vc, 1.0)))
    sl = frozenset(int(x) for x in b.loc[b.sess_last, 'hour'].unique())
    prev = [None]
    for row in b.itertuples():
        if row.td != prev[0]:
            eng.set_daily_ctx(DailyCtx(td=row.td, hvol=bool(row.hvol), member_z=np.nan,
                                       blocked=row.td in bm[sym], sess_last_hours=sl))
            prev[0] = row.td
        eng.on_bar(row.ts, float(row.open), float(row.high), float(row.low),
                   float(row.close), float(row.volume), float(row.raw_c), allow=bool(row.allow))
    return eng


allf = {}
for s in ['EB888', 'EG888']:
    eng = run_diagnose(s)
    ts = pd.to_datetime(data[s].ts)
    res = {}
    for (y, mo), v in sorted(eng._stat_m.items()):
        if y != 2026:
            continue
        mask = (ts.dt.year == y) & (ts.dt.month == mo)
        hv = float(data[s].loc[mask, 'hvol'].mean()) if mask.any() else float('nan')
        mm = f"{y}-{mo:02d}"
        res[mm] = dict(raw=int(v['raw']), p2=int(v['p2']), blk=int(v['blk']),
                       m1a=int(v['m1a']), m5=int(v['m5']), hvol=hv)
    # 2026 合计
    tot = dict(raw=0, p2=0, blk=0, m1a=0, m5=0)
    for v in res.values():
        for k in tot:
            tot[k] += v[k]
    tot['hvol'] = float(data[s].loc[ts.dt.year == 2026, 'hvol'].mean())
    res['_total_2026'] = tot
    allf[s] = res
    print(f'\n=== {s} 2026 ===')
    for mm, v in res.items():
        if mm.startswith('_'):
            continue
        print(f"  {mm}: raw={v['raw']} 过P2={v['p2']} 过M1a={v['m1a']} 成交={v['m5']} "
              f"高波={v['hvol']*100:.0f}%")
    print(f"  合计: raw={tot['raw']} 被P2砍={tot['raw']-tot['p2']} 被M1a砍={tot['p2']-tot['m1a']} 成交={tot['m5']}")

json.dump(allf, open('step58_funnel.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
print('\nsaved step58_funnel.json')
