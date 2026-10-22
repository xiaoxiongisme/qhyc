# -*- coding: utf-8 -*-
"""step74 V5X 旧参数 vs 推荐参数 对照（清洗后 BA 网格）

读 step73_sweep.json 挑推荐配置：
  - 退出三参数(SL/TRAIL/BE) 取 A 中 train 与 holdout 同正、MAR 综合最高者
  - 再在 B 中（围绕 A 中心）挑 COOLDOWN/ADD 综合最高者
然后在清洗后 BA 上跑 baseline 与 recommended 两套完整回测（含分品种），
输出 step74_compare.json + trades74_baseline.csv + trades74_reco.csv。
"""
import os, sys, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import step46_prod_bt as S

OUT = 'step73_sweep.json'
V5X_FLAGS = {'A1S', 'B3', 'M1a_pb', 'M5'}
BASELINE = dict(SL_ATR=2.0, TRAIL_ATR=2.0, BE_R=0.5, COOLDOWN=3,
                ADD_MAX_LOTS=2, ADD_THR=1.0)


def build_data():
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
    return data, costmap, tick, shock


def cfg_for(sym, scope, costmap, tick, shock):
    vc = sym[:-3]; cl = S.CLUSTER.get(vc)
    cfg = {'cost': costmap[vc], 'tick': tick.get(vc, 1.0),
           'blocked_tds': shock.get(cl, set()) if cl else set()}
    if scope and cl in scope:
        cfg['entry_mode'] = 'pullback'
    return cfg


def run_v5x(data, patch, costmap, tick, shock):
    saved = {k: getattr(S, k) for k in patch}
    for k, v in patch.items():
        setattr(S, k, v)
    try:
        tb = {s: S.run_symbol(s, data[s], cfg_for(s, S.P4_SECTORS, costmap, tick, shock),
                              V5X_FLAGS) for s in data}
        trades = [t for ts in tb.values() for t in ts]
    finally:
        for k, v in saved.items():
            setattr(S, k, v)
    return trades


def choose_reco(sweep):
    A = sweep.get('A', {})

    def sub_mar(t):
        m = t.get('mdd_yuan', 0); n = t.get('net_yuan', 0)
        return (n / abs(m)) if (m is not None and m < 0 and n is not None) else 0.0

    # A: 退出三参数（与 step73 一致：全样本 MAR 为主，要求 train&holdout 双正）
    cands = []
    for k, v in A.items():
        if 'error' in v or v.get('n', 0) == 0:
            continue
        tr = v.get('train', {}); ho = v.get('holdout', {})
        if (tr.get('net_yuan', 0) or 0) > 0 and (ho.get('net_yuan', 0) or 0) > 0:
            score = (v.get('mar') or 0)
            cands.append((score, k, v))
    cands.sort(reverse=True)
    if not cands:
        for k, v in A.items():
            if 'error' in v or v.get('n', 0) == 0:
                continue
            if (v.get('train', {}).get('net_yuan', 0) or 0) > 0:
                cands.append(((v.get('train', {}).get('net_yuan', 0) or 0), k, v))
        cands.sort(reverse=True)
    bestA_k = cands[0][1]
    m = re.match(r'SL([\d.]+)_TR([\d.]+)_BE([\d.]+)', bestA_k)
    exit_p = dict(SL_ATR=float(m.group(1)), TRAIL_ATR=float(m.group(2)),
                  BE_R=float(m.group(3)))
    # B: COOLDOWN/ADD（围绕 A 中心，同样用全样本 MAR）
    B = sweep.get('B', {})
    bcands = []
    for k, v in B.items():
        if 'error' in v or v.get('n', 0) == 0:
            continue
        tr = v.get('train', {}); ho = v.get('holdout', {})
        if (tr.get('net_yuan', 0) or 0) > 0 and (ho.get('net_yuan', 0) or 0) > 0:
            score = (v.get('mar') or 0)
            bcands.append((score, k, v))
    bcands.sort(reverse=True)
    cd_p = {}
    if bcands:
        mk = re.match(r'CD([\d.]+)_AL([\d.]+)_AT([\d.]+)', bcands[0][1])
        cd_p = dict(COOLDOWN=int(mk.group(1)), ADD_MAX_LOTS=int(mk.group(2)),
                    ADD_THR=float(mk.group(3)))
    reco = dict(BASELINE); reco.update(exit_p); reco.update(cd_p)
    return reco, bestA_k, (bcands[0][1] if bcands else None)


def agg(trades, label):
    import pandas as pd
    df = pd.DataFrame(trades)
    out = S.metrics(trades, label)
    # 分品种
    persym = {}
    for s in sorted(df.sym.unique()):
        mt = S.metrics([t for t in trades if t['sym'] == s], s)
        persym[s] = dict(n=mt.get('n'), net=round(mt.get('net_yuan', 0), 1),
                         pf=round(mt.get('pf', float('nan')), 3),
                         win=round(mt.get('win', float('nan')), 4),
                         mdd=round(mt.get('mdd_yuan', 0), 1))
    out['per_sym'] = persym
    return out


def main():
    sweep = json.load(open(OUT, encoding='utf-8'))
    reco, aK, bK = choose_reco(sweep)
    print('chosen A:', aK, 'B:', bK, 'reco:', reco, flush=True)
    data, costmap, tick, shock = build_data()
    # baseline
    tr_b = run_v5x(data, BASELINE, costmap, tick, shock)
    mb = agg(tr_b, 'BASELINE')
    # reco
    tr_r = run_v5x(data, reco, costmap, tick, shock)
    mr = agg(tr_r, 'RECO')
    import pandas as pd
    pd.DataFrame(tr_b).to_csv('trades74_baseline.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(tr_r).to_csv('trades74_reco.csv', index=False, encoding='utf-8-sig')
    out = dict(reco=reco, a_key=aK, b_key=bK, baseline=mb, reco_full=mr,
               delta=dict(
                   net=round(mr['net_yuan'] - mb['net_yuan'], 1),
                   mdd=round(mr['mdd_yuan'] - mb['mdd_yuan'], 1),
                   pf=round((mr.get('pf') or 0) - (mb.get('pf') or 0), 3),
                   mar=round((mr.get('mar') or 0) - (mb.get('mar') or 0), 3)))
    json.dump(out, open('step74_compare.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: out[k] for k in ['reco', 'delta']}, ensure_ascii=False, indent=1, default=str),
          flush=True)
    print('saved step74_compare.json', flush=True)


if __name__ == '__main__':
    main()
