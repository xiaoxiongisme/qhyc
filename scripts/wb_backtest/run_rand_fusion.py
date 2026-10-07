# -*- coding: utf-8 -*-
"""随机开仓 + 融合离场条件 基线对照。

实验设计（同一份数据 / 同一组合引擎 / 同一离场常数）：
  A. REAL 融合（加仓）：用 walk_fusion_states 真实信号（入场+离场+加码），add_max_lots=2。
  B. REAL 融合（1手）：同上信号但禁加码（add_max_lots=0），剥离加码层，只看"入场+离场"的 1 手收益。
  C. RANDOM + 融合离场（1手）：随机方向开仓（最小间隔、不重叠），离场用融合条件
       2×ATR 初始止损 / 2×ATR 吊灯 / 0.5×ATR 保本 / 30 天超时（与融合离场常数完全一致），
       禁加码。

比较 C 与 B：若 C ≈ B，则离场条件本身不创造 Alpha（随机开仓也能赚）；
若 C ≈ 0（扣费后）而 B >> 0，则融合的 Alpha 主要来自"入场"（方向择时），离场只是风控。

离场常数取自融合 V3.4 定稿：sl_atr=2.0 / trail_atr=2.0 / be_r=0.5（与 walk_fusion_states L244-281 逐位一致）。
"""
import os
import sys
import time
import pickle
import json
import random

sys.path.insert(0, '/app')
sys.path.insert(0, '/tmp')

import numpy as np
import pandas as pd
from types import SimpleNamespace

import run_four as F
import run_param_scan_v3 as V

OUT = '/tmp/randfusion_out'
os.makedirs(OUT, exist_ok=True)
CUT = np.datetime64('2021-01-01')          # OOS 2021-2026
SMOKE = '--smoke' in sys.argv
SEED = 20261007

# 融合离场常数（V3.4）
FUSION_P = SimpleNamespace(sl_atr=2.0, trail_atr=2.0, be_r=0.5,
                           ma_n=20, atr_n=14, add_max_lots=2,
                           W=60, entry_mode='both_nm', cooldown_bars=3,
                           adx_n=14, adx_min=15.0, use_sbull=False,
                           fib_confl=True, fib_ratios=(0.382, 0.5, 0.618),
                           fib_tol_atr=0.5, add_thr_atr=1.0, add_guard_atr=0.0)
MAX_HOLD_DAYS = 30                         # 与融合回测 max_hold=30 一致
RNG = random.Random(SEED)


# ---------- 融合离场扫描（单笔随机开仓 → 找离场） ----------
def fusion_exit_scan(sd, i0, dirn):
    """给定开仓 bar i0、方向 dirn(±1)，按融合离场条件找到离场 bar/价/原因。
    使用 sd['c']（后复权收盘价，与融合回测口径一致）；e_atr = sd['atr'][i0]（atr14/atr_n=14）。
    返回 (exit_i, exit_px, hold_days, reason)；若 e_atr 无效返回 (None,None,None,None)。"""
    c = sd['c']; days = sd['days']; atr = sd['atr']; n = len(c)
    entry_px = c[i0]; e_atr = atr[i0]
    if not (e_atr > 0) or np.isnan(e_atr):
        return None, None, None, None
    peak = entry_px; trough = entry_px; be_done = False
    sl = FUSION_P.sl_atr; tr = FUSION_P.trail_atr; be = FUSION_P.be_r
    for j in range(i0 + 1, n):
        cj = c[j]
        if dirn == 1:
            if cj > peak:
                peak = cj
            st = entry_px - sl * e_atr
            if tr > 0:
                t = peak - tr * e_atr
                if t > st:
                    st = t
            if be > 0 and (cj - entry_px) >= be * e_atr:
                be_done = True
            if be_done and entry_px > st:
                st = entry_px
            if cj <= st:
                return j, cj, int((days[j] - days[i0]) / np.timedelta64(1, 'D')), 'STOP'
        else:
            if cj < trough:
                trough = cj
            st = entry_px + sl * e_atr
            if tr > 0:
                t = trough + tr * e_atr
                if t < st:
                    st = t
            if be > 0 and (entry_px - cj) >= be * e_atr:
                be_done = True
            if be_done and entry_px < st:
                st = entry_px
            if cj >= st:
                return j, cj, int((days[j] - days[i0]) / np.timedelta64(1, 'D')), 'STOP'
        hd = int((days[j] - days[i0]) / np.timedelta64(1, 'D'))
        if hd >= MAX_HOLD_DAYS:
            return j, cj, hd, 'TIMEOUT'
    return n - 1, c[n - 1], int((days[n - 1] - days[i0]) / np.timedelta64(1, 'D')), 'FORCE_END'


def build_random_events(sd, gap_lo=20, gap_hi=220):
    """每品种随机开仓：从 OOS 起点起，到达 next_allowed 时随机方向开 1 手，
    用融合离场找平仓，并将下一允许开仓设在平仓之后 gap 根，保证不重叠。"""
    days = sd['days']; n = len(days); cut = np.datetime64(sd['sym_cut'])
    events = []
    next_allowed = 0
    for i in range(n):
        d = days[i]
        if d < CUT or d < cut:
            continue
        if i < next_allowed:
            continue
        dirn = RNG.choice([1, -1])
        exi, exp, hd, reason = fusion_exit_scan(sd, i, dirn)
        if exi is None:
            next_allowed = i + int(RNG.randint(gap_lo, gap_hi))
            continue
        events.append(dict(dt=sd['dts'][i], rank=2, sym=sd['sym'], kind='ENTRY',
                           dir=int(dirn), px_raw=float(sd['raw_close'][i]),
                           px_adj=float(sd['c'][i]), score=0.0, d=d,
                           _close_map=sd['close_map']))
        ed = days[exi]
        events.append(dict(dt=sd['dts'][exi], rank=0, sym=sd['sym'], kind='EXIT',
                           px_raw=float(sd['raw_close'][exi]), px_adj=float(sd['c'][exi]),
                           d=ed, _close_map=sd['close_map']))
        next_allowed = exi + int(RNG.randint(gap_lo, gap_hi))
    return events


def real_fusion_events(sd, add_max_lots):
    """真实融合信号事件（复刻 run_param_scan_v3.gen_events，但用 FUSION_P 直传，绕开 stop_scale）。"""
    sym = sd['sym']
    events = []
    prev_state, prev_lots, prev_entry_i = 0, 0, -1
    k = 0
    for fr in V.walk_fusion_states(sd['o'], sd['h'], sd['l'], sd['c'],
                                   sd['htf_dir'], FUSION_P):
        i = k + 2
        k += 1
        d = sd['days'][i]
        if d < CUT:
            continue
        st, lots, ei = fr['state'], fr['lots'], fr['entry_i']
        dt = sd['dts'][i]
        px_raw = float(sd['raw_close'][i]); px_adj = float(sd['c'][i])
        if st == 0:
            if prev_state != 0:
                events.append(dict(dt=dt, rank=0, sym=sym, kind='EXIT',
                                   px_raw=px_raw, px_adj=px_adj, d=d,
                                   _close_map=sd['close_map']))
            prev_state, prev_lots = 0, 0
            continue
        new_entry = (ei != prev_entry_i)
        if new_entry:
            if prev_state != 0:
                events.append(dict(dt=dt, rank=0, sym=sym, kind='EXIT',
                                   px_raw=px_raw, px_adj=px_adj, d=d,
                                   _close_map=sd['close_map']))
            dirn = st
            # 复刻 run_param_scan_v3.gen_events 的真实入场 score（决定 cap 阻塞优先级）
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
                               px_raw=px_raw, px_adj=px_adj, score=score, d=d,
                               _close_map=sd['close_map']))
        elif lots > prev_lots and prev_state != 0:
            events.append(dict(dt=dt, rank=1, sym=sym, kind='ADD', dir=st,
                               px_raw=px_raw, px_adj=px_adj,
                               n_add=lots - prev_lots, d=d,
                               _close_map=sd['close_map']))
        prev_state, prev_lots, prev_entry_i = st, lots, ei
    return events


def mkt_sum(trades, label):
    m = F.summarize(trades, label, MAX_HOLD_DAYS)
    return m


def load_data():
    V._isolate_cost_connections()
    engine = V.get_engine()
    universe, skipped = V.build_universe(engine)
    if os.path.exists(V.CACHE):
        with open(V.CACHE, 'rb') as f:
            data = pickle.load(f)
        if set(data) != set(universe):
            data = None
    else:
        data = None
    if data is None:
        data = {}
        for sym in universe:
            sd = V.load_symbol(sym, engine)
            if sd is not None:
                data[sym] = sd
        with open(V.CACHE, 'wb') as f:
            pickle.dump(data, f)
    margin, _ = V.load_margin_map(engine, universe)
    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    return data, days_global, margin, universe


def main():
    t0 = time.time()
    data, days_global, margin, universe = load_data()
    if SMOKE:
        data = {k: v for i, k in enumerate(sorted(data)) if i < 8 for kk, v in [(k, data[k])]}
    print('data=%d days=%d OOS_days=%d' % (
        len(data), len(days_global), int((days_global >= CUT).sum())), flush=True)
    days_oos = days_global[days_global >= CUT]

    # C. RANDOM + 融合离场（1手）—— 受限组合（max_products=20，与 REAL 同口径）
    ev_rand = []
    for sym, sd in data.items():
        ev_rand.extend(build_random_events(sd))
    sim_rand = F.simulate(ev_rand, days_oos, margin=margin, cap=True,
                          max_products=20, max_hold=MAX_HOLD_DAYS, add_max_lots=0)
    m_rand = mkt_sum(sim_rand['trades'], 'RAND')
    m_rand['peak_margin'] = sim_rand['peak_margin']
    m_rand['suggest_capital'] = round(sim_rand['peak_margin'] + abs(m_rand['mdd']), 0)
    pd.DataFrame(sim_rand['trades']).to_csv(os.path.join(OUT, 'rand_trades.csv'),
                                            index=False, encoding='utf-8-sig')
    print('[C RAND-cap20] n=%d net=%.0f pf=%.3f win=%.1f%% cost=%.0f MDD=%.0f 建议%.0f (%.0fs)' % (
        m_rand['n'], m_rand['net'], m_rand['pf'], m_rand['win'], m_rand['cost'],
        m_rand['mdd'], m_rand['suggest_capital'], time.time() - t0), flush=True)

    # C2. RANDOM + 融合离场（1手）—— 无组合限制（隔离纯离场期望，排除名额竞争干扰）
    sim_rand0 = F.simulate(ev_rand, days_oos, margin=margin, cap=False,
                           max_products=9999, max_hold=MAX_HOLD_DAYS, add_max_lots=0)
    m_rand0 = mkt_sum(sim_rand0['trades'], 'RAND0')
    m_rand0['peak_margin'] = sim_rand0['peak_margin']
    print('[C2 RAND-nocap] n=%d net=%.0f pf=%.3f win=%.1f%% cost=%.0f MDD=%.0f (%.0fs)' % (
        m_rand0['n'], m_rand0['net'], m_rand0['pf'], m_rand0['win'], m_rand0['cost'],
        m_rand0['mdd'], time.time() - t0), flush=True)

    # B. REAL 融合（1手，禁加码）
    ev_real1 = []
    for sym, sd in data.items():
        ev_real1.extend(real_fusion_events(sd, 0))
    sim_r1 = F.simulate(ev_real1, days_oos, margin=margin, cap=True,
                        max_products=20, max_hold=MAX_HOLD_DAYS, add_max_lots=0)
    m_r1 = mkt_sum(sim_r1['trades'], 'REAL1')
    m_r1['peak_margin'] = sim_r1['peak_margin']
    m_r1['suggest_capital'] = round(sim_r1['peak_margin'] + abs(m_r1['mdd']), 0)
    print('[B REAL1] n=%d net=%.0f pf=%.3f win=%.1f%% cost=%.0f MDD=%.0f (%.0fs)' % (
        m_r1['n'], m_r1['net'], m_r1['pf'], m_r1['win'], m_r1['cost'],
        m_r1['mdd'], time.time() - t0), flush=True)

    # A. REAL 融合（加仓，add_max_lots=2）
    ev_real2 = []
    for sym, sd in data.items():
        ev_real2.extend(real_fusion_events(sd, 2))
    sim_r2 = F.simulate(ev_real2, days_oos, margin=margin, cap=True,
                        max_products=20, max_hold=MAX_HOLD_DAYS, add_max_lots=2)
    m_r2 = mkt_sum(sim_r2['trades'], 'REAL2')
    m_r2['peak_margin'] = sim_r2['peak_margin']
    m_r2['suggest_capital'] = round(sim_r2['peak_margin'] + abs(m_r2['mdd']), 0)
    print('[A REAL2] n=%d net=%.0f pf=%.3f win=%.1f%% cost=%.0f MDD=%.0f (%.0fs)' % (
        m_r2['n'], m_r2['net'], m_r2['pf'], m_r2['win'], m_r2['cost'],
        m_r2['mdd'], time.time() - t0), flush=True)

    out = dict(cut=str(CUT), n_syms=len(data), oos_days=int((days_global >= CUT).sum()),
               RAND={k: m_rand[k] for k in ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                            'suggest_capital', 'peak_margin', 'per_trade',
                                            'n_syms', 'by_year', 'top5', 'worst5')},
               RAND_nocap={k: m_rand0[k] for k in ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                                   'per_trade', 'n_syms', 'by_year')},
               REAL1={k: m_r1[k] for k in ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                            'suggest_capital', 'peak_margin')},
               REAL2={k: m_r2[k] for k in ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                            'suggest_capital', 'peak_margin')})
    with open(os.path.join(OUT, 'randfusion.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print('DONE %.0fs' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
