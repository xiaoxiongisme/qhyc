# -*- coding: utf-8 -*-
"""海龟 + 融合 叠加回测（问题2实证）
S0 基线锚定：turtle 独立（IS 8格选参→OOS）、fusion 独立（固定基线参数）
S1 组合层并行：两策略事件合并共享名额 cap=20 / cap=40；两策略 OOS 日 pnl 相关性
S2a 信号层互滤：融合日线方向过滤海龟 ENTRY（use_ema=0/1 两版，检测"恒等变换"）
S2b 海龟突破确认融合：融合 ENTRY 需过去 5 日内同向 20 日 Donchian 突破
S3 离场层叠加：海龟入场/加仓 + 融合日线方向离场（保留 2N 灾难止损）
全流程 IS 2015-2020 → OOS 2021-2026；同引擎(run_four.simulate)同成本同保证金。
"""
import os
import sys
import time
import json
import pickle
import numpy as np
import pandas as pd

sys.path.insert(0, '/tmp')
sys.path.insert(0, '/app')
import run_param_scan_v3 as V
import run_four as R4
import run_docopt as RD          # 复用 gen_events_full / per_prod_is / oos_run / sel_of

RD.OUT = '/tmp/stack_out'
os.makedirs(RD.OUT, exist_ok=True)
OUT = RD.OUT
CUT = RD.CUT
SMOKE = '--smoke' in sys.argv


# ============================================================
# 融合单遍：事件 + 日线方向（避免 walk 两次）
# ============================================================
def gen_fusion_both(sd, Prun):
    sym = sd['sym']
    cutn = np.datetime64(sd['sym_cut'])
    events = []
    dir_by_day = {}
    prev_state, prev_lots, prev_entry_i = 0, 0, -1
    k = 0
    for fr in V.walk_fusion_states(sd['o'], sd['h'], sd['l'], sd['c'],
                                   sd['htf_dir'], Prun):
        i = k + 2
        k += 1
        d = sd['days'][i]
        st, lots, ei = fr['state'], fr['lots'], fr['entry_i']
        dir_by_day[d] = 0 if st == 0 else (1 if st == 1 else -1)
        if d < cutn:
            continue
        dt = sd['dts'][i]
        px_raw = float(sd['raw_close'][i]); px_adj = float(sd['c'][i])
        if st == 0:
            if prev_state != 0:
                events.append(dict(dt=dt, rank=0, sym=sym, kind='EXIT',
                                   px_raw=px_raw, px_adj=px_adj, d=d))
            prev_state, prev_lots = 0, 0
            continue
        new_entry = (ei != prev_entry_i)
        if new_entry:
            if prev_state != 0:
                events.append(dict(dt=dt, rank=0, sym=sym, kind='EXIT',
                                   px_raw=px_raw, px_adj=px_adj, d=d))
            dirn = st
            adx_v = float(sd['adx'][i]); atr_v = float(sd['atr'][i])
            c_v = float(sd['c'][i])
            c20 = float(sd['c'][i - 20]) if i >= 20 else c_v
            if atr_v > 0:
                mom = dirn * (c_v - c20) / (20.0 * atr_v)
                close_n = 1.0 - abs(c_v - float(sd['ma20'][i])) / (1.5 * atr_v)
            else:
                mom = close_n = 0.0
            score = (0.5 * min(max(adx_v / 40.0, 0.0), 1.0)
                     + 0.3 * min(max(mom, 0.0), 1.0)
                     + 0.2 * min(max(close_n, 0.0), 1.0))
            events.append(dict(dt=dt, rank=2, sym=sym, kind='ENTRY', dir=dirn,
                               px_raw=px_raw, px_adj=px_adj, score=score, d=d))
        elif lots > prev_lots and prev_state != 0:
            events.append(dict(dt=dt, rank=1, sym=sym, kind='ADD', dir=st,
                               px_raw=px_raw, px_adj=px_adj,
                               n_add=lots - prev_lots, d=d))
        prev_state, prev_lots, prev_entry_i = st, lots, ei
    for e in events:
        e['_close_map'] = sd['close_map']
    days = np.array(sorted(dir_by_day), dtype='datetime64[D]')
    dirs = np.array([dir_by_day[d] for d in days], dtype=np.int8)
    return events, days, dirs


def dir_lookup(fdays, fdirs):
    """返回 lookup(day)：day 之前最近一个融合日线方向（无则 None）。"""

    def lookup(day):
        i = int(np.searchsorted(fdays, np.datetime64(day, 'D'), side='left'))
        if i - 1 >= 0:
            return int(fdirs[i - 1])
        return None

    return lookup


# ============================================================
# 海龟 IS 扫描（8 格，逐品种无帽，与 run_four 同口径）
# ============================================================
TGRID = [dict(entry_ch=ec, exit_ch=xc, atr_n=20, use_ema=ue)
         for ec in (20, 55) for xc in (10, 20) for ue in (0, 1)]
TCFG = dict(max_hold=200, add_max=4, cap=20)


def turtle_is_scan(data, days_is, margin):
    per_combo = {}
    for p in TGRID:
        t0 = time.time()
        per = {}
        for sym, sd in data.items():
            ev = R4.gen_turtle(sd, p)
            ev = [e for e in ev if e['d'] < CUT]
            if not ev:
                continue
            sim = R4.simulate(ev, days_is, margin=margin, cap=False,
                              max_products=9999, max_hold=TCFG['max_hold'],
                              add_max_lots=TCFG['add_max'])
            df = pd.DataFrame(sim['trades'])
            net = float(df.pnl.sum()) if len(df) else 0.0
            if net or len(df):
                per[sym] = (net, len(df))
        tot = sum(v[0] for v in per.values())
        per_combo[tuple(sorted(p.items()))] = (p, tot, per)
        print('[IS][turtle] %s total=%.0f n_prod=%d (%.0fs)'
              % (p, tot, len(per), time.time() - t0), flush=True)
    bk = max(per_combo, key=lambda k: per_combo[k][1])
    p, tot, per = per_combo[bk]
    sel = RD.sel_of(per)
    print('[IS][turtle] BEST %s total=%.0f selected=%d' % (p, tot, len(sel)),
          flush=True)
    return p, tot, per, sel


# ============================================================
# S2a：融合日线方向过滤海龟 ENTRY
# ============================================================
def gate_turtle_by_fdir(evs, dlook):
    out = []
    drop = 0
    for e in evs:
        if e['kind'] == 'ENTRY':
            fd = dlook(e['d'])
            if fd != e['dir']:
                drop += 1
                continue
        out.append(e)
    return out, drop


# ============================================================
# S2b：海龟 20 日突破确认融合 ENTRY（过去 5 日内同向突破）
# ============================================================
def breakout_flags(sd, n=20):
    g = R4.daily_ohlc(sd)
    H = R4.ffb(g['h'].values.astype(float))
    L = R4.ffb(g['l'].values.astype(float))
    days = g.index.values
    hu = R4.roll_max(H, n)
    lu = R4.roll_min(L, n)
    fl = []
    for i in range(len(H)):
        f = 0
        if i >= n + 1 and not np.isnan(hu[i - 1]) and not np.isnan(lu[i - 1]):
            if H[i] >= hu[i - 1]:
                f = 1
            elif L[i] <= lu[i - 1]:
                f = -1
        fl.append(f)
    d = np.array([np.datetime64(x, 'D') for x in days])
    return d, np.array(fl, dtype=np.int8)


def gate_fusion_by_breakout(evs, bdays, bflags, win=5):
    out = []
    drop = 0
    for e in evs:
        if e['kind'] == 'ENTRY':
            d0 = np.datetime64(e['d'], 'D')
            d1 = d0 - np.timedelta64(win, 'D')
            i0 = int(np.searchsorted(bdays, d1, side='left'))
            i1 = int(np.searchsorted(bdays, d0, side='left'))
            ok = False
            for j in range(i0, i1):
                if int(bflags[j]) == e['dir']:
                    ok = True
                    break
            if not ok:
                drop += 1
                continue
        out.append(e)
    return out


# ============================================================
# S3：海龟入场/加仓 + 融合日线方向离场（保留 2N 灾难止损）
# ============================================================
def gen_turtle_fx(sd, p, fdays, fdirs):
    g = R4.daily_ohlc(sd)
    H = R4.ffb(g['h'].values.astype(float))
    L = R4.ffb(g['l'].values.astype(float))
    C = R4.ffb(g['c'].values.astype(float))
    Rr = R4.ffb(g['r'].values.astype(float))
    days = g.index.values
    nd = len(H)
    if nd < 250:
        return []
    atr_v = R4.atr(H, L, C, p['atr_n'])
    he = R4.roll_max(H, p['entry_ch'])
    le = R4.roll_min(L, p['entry_ch'])
    ema200 = R4.ema(C, 200) if p['use_ema'] else np.full(nd, np.nan)
    cut = np.datetime64(sd['sym_cut'])
    cm = sd['close_map']

    def fd(i):
        j = int(np.searchsorted(fdays, days[i], side='left')) - 1
        return int(fdirs[j]) if j >= 0 else None

    ev = []
    pos = 0
    units = 0
    last_add = 0.0
    for i in range(max(p['entry_ch'], 10), nd):
        day = days[i]
        if day < cut:
            continue
        a = atr_v[i - 1] if i > 0 and not np.isnan(atr_v[i - 1]) else atr_v[i]
        if np.isnan(a) or a <= 0:
            continue
        f = fd(i)
        if pos == 1:
            stop = last_add - 2.0 * a
            if L[i] <= stop or f != 1:
                ev.append(R4.mk_ev_day(sd['sym'], day, C[i], Rr[i], 'EXIT', 1, cm, 0))
                pos = 0; units = 0
        elif pos == -1:
            stop = last_add + 2.0 * a
            if H[i] >= stop or f != -1:
                ev.append(R4.mk_ev_day(sd['sym'], day, C[i], Rr[i], 'EXIT', -1, cm, 0))
                pos = 0; units = 0
        if pos == 0:
            if np.isnan(he[i - 1]) or np.isnan(le[i - 1]):
                continue
            if H[i] >= he[i - 1] and (np.isnan(ema200[i]) or C[i] > ema200[i]):
                ev.append(R4.mk_ev_day(sd['sym'], day, C[i], Rr[i], 'ENTRY', 1, cm, 2,
                                       (H[i] - he[i - 1]) / a))
                pos = 1; units = 1; last_add = C[i]
            elif L[i] <= le[i - 1] and (np.isnan(ema200[i]) or C[i] < ema200[i]):
                ev.append(R4.mk_ev_day(sd['sym'], day, C[i], Rr[i], 'ENTRY', -1, cm, 2,
                                       (le[i - 1] - L[i]) / a))
                pos = -1; units = 1; last_add = C[i]
        else:
            if pos == 1 and units < 4 and C[i] >= last_add + 0.5 * a:
                ev.append(R4.mk_ev_day(sd['sym'], day, C[i], Rr[i], 'ADD', 1, cm, 1,
                                       (C[i] - last_add) / a, n=1))
                units += 1; last_add = C[i]
            elif pos == -1 and units < 4 and C[i] <= last_add - 0.5 * a:
                ev.append(R4.mk_ev_day(sd['sym'], day, C[i], Rr[i], 'ADD', -1, cm, 1,
                                       (last_add - C[i]) / a, n=1))
                units += 1; last_add = C[i]
    return ev


# ============================================================
# 评估管线：全区间事件 → IS 逐品种 → sel → OOS
# ============================================================
def eval_variant(by_sym, days_is, days_oos, margin, cfg, label):
    per = RD.per_prod_is(by_sym, days_is, margin, cfg)
    sel = RD.sel_of(per)
    is_tot = sum(v[0] for v in per.values())
    oos = RD.oos_run(by_sym, sel, days_oos, margin, cfg, label)
    print('[%s] IS=%.0f sel=%d | OOS sel net=%.0f pf=%.3f win=%.1f mdd=%.0f cap=%.0f | '
          'all net=%.0f mdd=%.0f'
          % (label, is_tot, len(sel),
             oos['sel'].get('net', 0), oos['sel'].get('pf', 0),
             oos['sel'].get('win', 0), oos['sel'].get('mdd', 0),
             oos['sel'].get('suggest_capital', 0),
             oos['all'].get('net', 0), oos['all'].get('mdd', 0)), flush=True)
    return dict(is_total=round(is_tot, 0), n_sel=len(sel), sel=oos['sel'],
                all=oos['all'])


# ============================================================
def main():
    V._isolate_cost_connections()
    engine = V.get_engine()
    t0 = time.time()
    universe, skipped = V.build_universe(engine)
    if SMOKE:
        universe = universe[:5]
    print('universe=%d skipped=%d' % (len(universe), len(skipped)), flush=True)

    data = None
    if os.path.exists(V.CACHE) and not SMOKE:
        try:
            with open(V.CACHE, 'rb') as f:
                data = pickle.load(f)
            if set(data.keys()) != set(universe):
                data = None
        except Exception:
            data = None
    if data is None:
        data = {}
        for sym in universe:
            try:
                sd = V.load_symbol(sym, engine)
            except Exception as ex:
                print('LOAD-ERR %s: %s' % (sym, str(ex)[:100]), flush=True)
                sd = None
            if sd is not None:
                data[sym] = sd
        if not SMOKE:
            with open(V.CACHE, 'wb') as f:
                pickle.dump(data, f)
    print('data=%d syms (%.0fs)' % (len(data), time.time() - t0), flush=True)

    margin, _ = V.load_margin_map(engine, universe)
    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    days_is = days_global[days_global < CUT]
    days_oos = days_global[days_global >= CUT]
    print('days=%d OOS=%d' % (len(days_global), len(days_oos)), flush=True)

    results = dict(cut=str(CUT), n_syms=len(data), variants={})

    # ---------- S0a：海龟基线（IS 选参） ----------
    tp, tis, tper, tsel = turtle_is_scan(data, days_is, margin)
    t_ev = {}
    for sym, sd in data.items():
        t_ev[sym] = R4.gen_turtle(sd, tp)
    res_t = eval_variant(t_ev, days_is, days_oos, margin, TCFG, 'T0_turtle')
    results['turtle_best_p'] = tp
    results['variants']['T0_turtle'] = res_t

    # ---------- S0b：融合基线（固定参数） ----------
    FCFG = dict(max_hold=30, add_max=2, cap=20)   # cap 与海龟对齐=20（此前报告用10，另注明）
    Prun = V.make_P(sl_atr=4.0, trail_atr=2.0, be_r=0.5,
                    add_on_times=2, add_max_lots=1)
    fus_ev = {}
    fus_dir = {}
    tt = time.time()
    for sym, sd in data.items():
        try:
            ev, fd_, fdr = gen_fusion_both(sd, Prun)
            fus_ev[sym] = ev
            fus_dir[sym] = (fd_, fdr)
        except Exception as ex:
            print('FUS-ERR %s: %s' % (sym, str(ex)[:100]), flush=True)
    print('fusion built %d syms (%.0fs)' % (len(fus_ev), time.time() - tt), flush=True)
    res_f = eval_variant(fus_ev, days_is, days_oos, margin, FCFG, 'F0_fusion')
    results['variants']['F0_fusion'] = res_f

    # ---------- S1：组合层并行（合并事件流，共享名额） ----------
    # 两策略各自 sel：海龟 sel 来自 tper；融合 sel 重算
    per_f = RD.per_prod_is(fus_ev, days_is, margin, FCFG)
    sel_f = set(RD.sel_of(per_f))
    sel_t = set(tsel)
    union_sel = sel_t | sel_f
    results['sel_turtle'] = sorted(sel_t)
    results['sel_fusion'] = sorted(sel_f)
    results['sel_union'] = sorted(union_sel)

    def norm_dt(e):
        ts = pd.Timestamp(e['dt'])
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        e['dt'] = ts

    for cap in (20, 40):
        for tag, usesel in (('sel', True), ('all', False)):
            evs = []
            for sym in data:
                if usesel and sym not in union_sel:
                    continue
                for e in t_ev.get(sym, []):
                    if e['d'] >= CUT:
                        norm_dt(e)
                        evs.append(e)
                for e in fus_ev.get(sym, []):
                    if e['d'] >= CUT:
                        norm_dt(e)
                        evs.append(e)
            sim = R4.simulate(evs, days_oos, margin=margin, cap=True,
                              max_products=cap, max_hold=200, add_max_lots=4)
            m = R4.summarize(sim['trades'], 'S1_merge_cap%d_%s' % (cap, tag), 200)
            m['peak_margin'] = sim['peak_margin']
            m['suggest_capital'] = round(sim['peak_margin'] + abs(m.get('mdd', 0)), 0)
            results['variants']['S1_merge_cap%d_%s' % (cap, tag)] = m
            print('[S1 cap=%d %s] net=%.0f pf=%.3f win=%.1f n=%d mdd=%.0f cap=%.0f'
                  % (cap, tag, m['net'], m['pf'], m['win'], m['n'], m['mdd'],
                     m['suggest_capital']), flush=True)

    # 两策略 OOS 日 pnl 相关（sel 口径，按 exit_date 聚合）
    def daily_pnl(label, tag):
        fp = os.path.join(OUT, 'oos_%s_%s.csv' % (label, tag))
        if not os.path.exists(fp):
            return None
        df = pd.read_csv(fp)
        if not len(df):
            return None
        return df.groupby('exit_date')['pnl'].sum()

    d_t = daily_pnl('T0_turtle', 'sel')
    d_f = daily_pnl('F0_fusion', 'sel')
    if d_t is not None and d_f is not None:
        j = pd.concat([d_t, d_f], axis=1, keys=['t', 'f']).fillna(0.0)
        r = float(np.corrcoef(j['t'], j['f'])[0, 1])
        results['daily_pnl_corr_turtle_fusion'] = round(r, 3)
        print('[corr] daily pnl r=%.3f (n=%d days)' % (r, len(j)), flush=True)

    # ---------- S2a：融合方向过滤海龟 ----------
    for ue in (0, 1):
        p2 = dict(tp); p2['use_ema'] = ue
        by_sym = {}
        drops = 0
        for sym, sd in data.items():
            ev = R4.gen_turtle(sd, p2)
            dl = dir_lookup(*fus_dir[sym])
            ev2, dp = gate_turtle_by_fdir(ev, dl)
            drops += dp
            by_sym[sym] = ev2
        res = eval_variant(by_sym, days_is, days_oos, margin, TCFG,
                           'S2a_fdir_ue%d' % ue)
        res['dropped_entries'] = drops
        results['variants']['S2a_fdir_ue%d' % ue] = res

    # 恒等变换检测：S2a(ue=1) 与 T0(ue=1) 的 ENTRY 事件重合度
    p1 = dict(tp); p1['use_ema'] = 1
    n_diff = 0
    n_t1 = 0
    for sym, sd in data.items():
        e_base = {id(e) for e in []}
        ev1 = [e for e in R4.gen_turtle(sd, p1) if e['kind'] == 'ENTRY'
               and e['d'] >= CUT]
        ev2 = [e for e in by_sym.get(sym, []) if e['kind'] == 'ENTRY'
               and e['d'] >= CUT]
        n_t1 += len(ev1)
        k1 = {(e['sym'], str(e['d']), e['dir']) for e in ev1}
        k2 = {(e['sym'], str(e['d']), e['dir']) for e in ev2}
        n_diff += len(k1 ^ k2)
    results['s2a_identity_check'] = dict(
        n_entry_ema1_oos=n_t1, n_diff_keys=n_diff,
        note='n_diff=0 ⇒ 融合方向门控与 EMA200 过滤完全等价（恒等变换）')
    print('[S2a identity] entries(ue=1,OOS)=%d diff_keys=%d'
          % (n_t1, n_diff), flush=True)

    # ---------- S2b：海龟突破确认融合 ENTRY ----------
    by_sym = {}
    drops = 0
    for sym, sd in data.items():
        bd, bf = breakout_flags(sd, 20)
        ev2 = gate_fusion_by_breakout(fus_ev.get(sym, []), bd, bf, 5)
        drops += len(fus_ev.get(sym, [])) - len(ev2)
        by_sym[sym] = ev2
    res = eval_variant(by_sym, days_is, days_oos, margin, FCFG, 'S2b_brk_fusion')
    res['dropped_entries'] = drops
    results['variants']['S2b_brk_fusion'] = res

    # ---------- S3：海龟入场 + 融合方向离场 ----------
    by_sym = {}
    for sym, sd in data.items():
        by_sym[sym] = gen_turtle_fx(sd, tp, *fus_dir[sym])
    res = eval_variant(by_sym, days_is, days_oos, margin, TCFG, 'S3_turtle_fexit')
    results['variants']['S3_turtle_fexit'] = res

    with open(os.path.join(OUT, 'stack_results.json'), 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print('DONE %.0fs' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
