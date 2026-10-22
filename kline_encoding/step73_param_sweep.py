# -*- coding: utf-8 -*-
"""step73 V5X 持仓周期参数扫描（在清洗后 bar_60m_ba 上）

复用 step46 引擎：import 后打补丁其模块级全局参数（SL_ATR/TRAIL_ATR/BE_R/
COOLDOWN/ADD_MAX_LOTS/ADD_THR），不改引擎逻辑、不改数据表结构。
V5X = flags{A1S,B3,M1a_pb,M5} + scope=P4_SECTORS（化工链条回踩），无 A2。

两阶段：
  A) 离场三参数主网格 SL×TRAIL×BE（COOLDOWN/ADD 固定）
  B) 在 A 最优区附近扫 COOLDOWN×ADD_MAX_LOTS×ADD_THR
按 train(<=2025)/holdout(>=2026)/oos21(>=2021) 评估 PF/MAR/MDD/win/net。
增量落盘 step73_sweep.json（每组合立即写，防崩溃丢进度）。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json, time, itertools
import numpy as np
import step46_prod_bt as S

OUT = 'step73_sweep.json'
HOLD_PARAMS = ['SL_ATR', 'TRAIL_ATR', 'BE_R', 'COOLDOWN', 'ADD_MAX_LOTS', 'ADD_THR']
V5X_FLAGS = {'A1S', 'B3', 'M1a_pb', 'M5'}
BASELINE = dict(SL_ATR=2.0, TRAIL_ATR=2.0, BE_R=0.5, COOLDOWN=3,
                ADD_MAX_LOTS=2, ADD_THR=1.0)


def build_data():
    print('load_all ...', flush=True)
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
            print('skip', s, type(e).__name__, e, flush=True)
    print(f'loaded {len(data)} syms', flush=True)
    data = S.attach_member_z(data, fac)
    shock = S.cluster_shock_days(data)
    return data, costmap, tick, shock


def cfg_for(sym, scope, costmap, tick, shock):
    vc = sym[:-3]
    cl = S.CLUSTER.get(vc)
    cfg = {'cost': costmap[vc], 'tick': tick.get(vc, 1.0),
           'blocked_tds': shock.get(cl, set()) if cl else set()}
    if scope and cl in scope:
        cfg['entry_mode'] = 'pullback'
    return cfg


def run_v5x(data, patch, costmap, tick, shock):
    saved = {}
    for k in HOLD_PARAMS:
        saved[k] = getattr(S, k)
        setattr(S, k, patch[k])
    try:
        tb = {}
        for s in data:
            tb[s] = S.run_symbol(s, data[s],
                                 cfg_for(s, S.P4_SECTORS, costmap, tick, shock),
                                 V5X_FLAGS)
        trades = [t for ts in tb.values() for t in ts]
        m = S.metrics(trades, 'V5X')
    finally:
        for k, v in saved.items():
            setattr(S, k, v)
    return m


def slim(m):
    """只保留用于排序/报告的字段"""
    out = dict(n=m.get('n'), net_yuan=round(m.get('net_yuan', 0), 1),
               mdd_yuan=round(m.get('mdd_yuan', 0), 1),
               pf=round(m.get('pf', float('nan')), 3),
               win=round(m.get('win', float('nan')), 4),
               mar=round(m.get('mar', float('nan')), 3),
               avg_r=round(m.get('avg_r', float('nan')), 4))
    for tag in ('train', 'holdout', 'oos21'):
        if tag in m:
            t = m[tag]
            out[tag] = dict(n=t.get('n'),
                            net_yuan=round(t.get('net_yuan', 0), 1),
                            mdd_yuan=round(t.get('mdd_yuan', 0), 1),
                            pf=round(t.get('pf', float('nan')), 3),
                            win=round(t.get('win', float('nan')), 4),
                            mar=round(t.get('mar', float('nan')), 3))
    return out


def main():
    data, costmap, tick, shock = build_data()
    try:
        res = json.load(open(OUT, encoding='utf-8'))
    except Exception:
        res = {'baseline': None, 'A': {}, 'B': {}, 'meta': {}}
    res['meta'] = dict(engine='step46 V5X (A1S,B3,M1a_pb,M5)+P4_SECTORS',
                       data='bar_60m_ba (cleaned NaN-fix)',
                       hold_params=HOLD_PARAMS, baseline=BASELINE)
    res.setdefault('A', {})
    res.setdefault('B', {})

    # ---- 基线 ----
    if res.get('baseline') is None:
        t0 = time.time()
        m = run_v5x(data, BASELINE, costmap, tick, shock)
        res['baseline'] = slim(m)
        res['baseline']['secs'] = round(time.time() - t0, 2)
        json.dump(res, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('baseline', json.dumps(res['baseline'], ensure_ascii=False), flush=True)

    # ---- A: SL×TRAIL×BE（COOLDOWN/ADD 固定为基线）----
    sl = [1.5, 2.0, 2.5, 3.0]
    tr = [1.5, 2.0, 2.5, 3.0]
    be = [0.3, 0.5, 0.8, 1.0]
    done_A = set(res.get('A', {}).keys())
    for a, b, c in itertools.product(sl, tr, be):
        key = f'SL{a}_TR{b}_BE{c}'
        if key in done_A:
            continue
        patch = dict(BASELINE); patch.update(SL_ATR=a, TRAIL_ATR=b, BE_R=c)
        t0 = time.time()
        try:
            m = run_v5x(data, patch, costmap, tick, shock)
            rec = slim(m); rec['secs'] = round(time.time() - t0, 2)
            res['A'][key] = rec
        except Exception as e:
            res['A'][key] = dict(error=f'{type(e).__name__}: {e}')
        json.dump(res, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(key, json.dumps(rec if 'error' not in rec else rec, ensure_ascii=False),
              flush=True)

    # ---- B: 在 A 最优区附近扫 COOLDOWN×ADD_MAX_LOTS×ADD_THR ----
    # 先用 net/mar 综合挑 A 中 holdout 与 train 同正且 MAR 较高者，取其 SL/TRAIL/BE 中心
    best = pick_center_A(res.get('A', {}))
    print('A center ->', best, flush=True)
    res.setdefault('B', {})
    cd = [1, 2, 3, 5, 7]
    al = [1, 2, 3]
    at = [0.5, 1.0, 1.5]
    done_B = set(res.get('B', {}).keys())
    for a, b, c in itertools.product(cd, al, at):
        key = f'CD{a}_AL{b}_AT{c}'
        if key in done_B:
            continue
        patch = dict(best); patch.update(COOLDOWN=a, ADD_MAX_LOTS=b, ADD_THR=c)
        t0 = time.time()
        try:
            m = run_v5x(data, patch, costmap, tick, shock)
            rec = slim(m); rec['secs'] = round(time.time() - t0, 2)
            rec['base'] = best
            res['B'][key] = rec
        except Exception as e:
            res['B'][key] = dict(error=f'{type(e).__name__}: {e}')
        json.dump(res, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(key, json.dumps(rec if 'error' not in rec else rec, ensure_ascii=False),
              flush=True)

    json.dump(res, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('DONE step73 sweep ->', OUT, flush=True)


def sub_mar(t):
    m = t.get('mdd_yuan', 0); n = t.get('net_yuan', 0)
    return (n / abs(m)) if (m is not None and m < 0 and n is not None) else 0.0


def pick_center_A(A):
    """挑 A 中 train 与 holdout 都 net>0 且「全样本 MAR」最大者，取其 SL/TRAIL/BE 作为 B 中心。
    用全样本 MAR（稳健，已存于结果）而非分时段 MAR（2026 仅 226 笔、窗口 MDD 不稳定，会失真）。"""
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
    if not cands:
        return dict(BASELINE)
    _, k, v = cands[0]
    import re
    m = re.match(r'SL([\d.]+)_TR([\d.]+)_BE([\d.]+)', k)
    sl = float(m.group(1)); tr = float(m.group(2)); be = float(m.group(3))
    p = dict(BASELINE); p.update(SL_ATR=sl, TRAIL_ATR=tr, BE_R=be)
    return p


if __name__ == '__main__':
    main()
