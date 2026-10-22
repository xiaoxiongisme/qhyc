# -*- coding: utf-8 -*-
"""step57：用「价格连续 N 根维持 EMA140 同向」(dirpersist 体制门) 替代会员数据，
对 EB/EG 重做回测，并对比 M5(会员) 与 none(去体制门)。

核心看点：
  - EB/EG 在 2025-2026 被 M5 全拒导致 0 笔；dirpersist 不依赖会员数据，能否补回信号？
  - 扫描 N（ALT_THR）看信号数/PF 的权衡。
  - 全市场再确认 dirpersist 不是退化替代（对照 prior 200万结论）。
"""
import json
import numpy as np
import pandas as pd
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CLUSTER
from step49_winrate import replay

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))

SYMS = ['EB888', 'EG888']
data = {}
for s in SYMS:
    b = s46.build_sym(s, ba, raw, dd, var)
    if b is not None:
        data[s] = b
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)
bm = {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}

print('=== 数据年份跨度 ===')
for s in SYMS:
    b = data[s]
    yrs = b.td.dropna().astype(str).str[:4].astype(int)
    print(f'{s}: {yrs.min()}–{yrs.max()}, 共 {len(b)} 根 60m')

# ---- 配置：dirpersist 扫描 N + 对照 ----
NS = [1, 2, 3, 5, 8, 10, 15, 20]


def summarize(trades):
    if not trades:
        return dict(n=0, net=0.0, pf=np.nan, win=np.nan)
    df = pd.DataFrame(trades)
    w = df[df.net > 0]; l = df[df.net < 0]
    gp = float(w.net.sum()); gl = float(-l.net.sum()) if len(l) else 0.0
    eq = df.net.cumsum().values
    mdd = float((eq - np.maximum.accumulate(eq)).min())
    return dict(n=len(df), net=float(df.net.sum()),
                pf=float(gp / gl) if gl > 0 else np.nan,
                win=float(len(w) / len(df)),
                mdd=mdd,
                maxloss=float(df.net.min()))


def split_is_ho(trades):
    df = pd.DataFrame(trades)
    is_ = summarize([t for t in trades if t['year'] <= 2025])
    ho = summarize([t for t in trades if t['year'] >= 2026])
    return is_, ho


results = {}
print('\n=== EB / EG：dirpersist(N) vs M5(会员) vs none ===')
for s in SYMS:
    results[s] = {}
    print(f'\n----- {s} -----')
    print(f"{'config':16s} {'N':>3s} {'总笔':>5s} {'总净利':>10s} {'PF':>6s} {'胜率%':>6s} "
          f"{'IS笔':>5s} {'IS净利':>9s} {'HO笔':>4s} {'HO净利':>9s} {'2025笔':>6s} {'2026笔':>6s}")
    # 对照：M5 会员
    cfg = V5XConfig(); cfg.ALT_GATE = 'm5'; cfg.M5_ON = True; cfg.M5_Z_THR = 1.0
    tr = replay(s, data[s], cfg, costmap, tick, bm[s])
    is_, ho = split_is_ho(tr)
    y25 = sum(1 for t in tr if t['year'] == 2025)
    y26 = sum(1 for t in tr if t['year'] == 2026)
    m = summarize(tr)
    results[s]['M5_member'] = dict(m=m, is_=is_, ho=ho, y25=y25, y26=y26, N=None)
    print(f"{'M5(会员)':16s} {'-':>3s} {m['n']:5d} {m['net']:10.0f} "
          f"{str(round(m['pf'],2)) if m['pf']==m['pf'] else 'NA':>6s} {m['win']*100:6.1f} "
          f"{is_['n']:5d} {is_['net']:9.0f} {ho['n']:4d} {ho['net']:9.0f} {y25:6d} {y26:6d}")
    # 对照：none
    cfg = V5XConfig(); cfg.ALT_GATE = 'none'
    tr = replay(s, data[s], cfg, costmap, tick, bm[s])
    is_, ho = split_is_ho(tr)
    y25 = sum(1 for t in tr if t['year'] == 2025)
    y26 = sum(1 for t in tr if t['year'] == 2026)
    m = summarize(tr)
    results[s]['none'] = dict(m=m, is_=is_, ho=ho, y25=y25, y26=y26, N=None)
    print(f"{'none(去门)':16s} {'-':>3s} {m['n']:5d} {m['net']:10.0f} "
          f"{str(round(m['pf'],2)) if m['pf']==m['pf'] else 'NA':>6s} {m['win']*100:6.1f} "
          f"{is_['n']:5d} {is_['net']:9.0f} {ho['n']:4d} {ho['net']:9.0f} {y25:6d} {y26:6d}")
    # dirpersist 扫描
    for N in NS:
        cfg = V5XConfig(); cfg.ALT_GATE = 'dirpersist'; cfg.ALT_THR = float(N)
        tr = replay(s, data[s], cfg, costmap, tick, bm[s])
        is_, ho = split_is_ho(tr)
        y25 = sum(1 for t in tr if t['year'] == 2025)
        y26 = sum(1 for t in tr if t['year'] == 2026)
        m = summarize(tr)
        results[s][f'dirpersist_{N}'] = dict(m=m, is_=is_, ho=ho, y25=y25, y26=y26, N=N)
        print(f"{'dirpersist':16s} {N:3d} {m['n']:5d} {m['net']:10.0f} "
              f"{str(round(m['pf'],2)) if m['pf']==m['pf'] else 'NA':>6s} {m['win']*100:6.1f} "
              f"{is_['n']:5d} {is_['net']:9.0f} {ho['n']:4d} {ho['net']:9.0f} {y25:6d} {y26:6d}")

json.dump(results, open('step57_results.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
print('\nsaved step57_results.json')
