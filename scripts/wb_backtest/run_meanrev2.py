# -*- coding: utf-8 -*-
"""均值回归策略【新方向】walk-forward（15m 入场离场 + 日线方向过滤）。

相对 run_meanrev 的扩展：
  1) entry_k 加深到 3.0（更深的偏离才入场，过滤弱信号）；
  2) 新增 ADX(15m) 门控 adx_max：仅当 ADX <= adx_max（震荡市）才做均值回归，
     直击上一轮"长尾（6-13 天）被趋势吞噬"的死因；
  3) 保留 intraday 日内平仓变体。

IS = 2015-2020 选品+参数；OOS = 2021-2026 纯样本外。
复用 run_param_scan_v3 的 simulate 结构 / CostBook / 保证金 / 品种池；
共用 /tmp/mr15_cache.pkl（与 run_meanrev 同格式，建一次复用）。
"""
import os
import sys
import time
import pickle
import json

sys.path.insert(0, '/app')
sys.path.insert(0, '/tmp')

import numpy as np
import pandas as pd
from types import SimpleNamespace

import run_param_scan_v3 as V
from sqlalchemy import text as T

from app.data.back_adjust import apply_back_adjust, load_segments

OUT = '/tmp/mr2_out'
CACHE = '/tmp/mr15_cache.pkl'
os.makedirs(OUT, exist_ok=True)
CUT = np.datetime64('2021-01-01')
SMOKE = '--smoke' in sys.argv
IS_ONLY = '--is' in sys.argv
OOS_ONLY = '--oos' in sys.argv

CB = V.CB


# ============================================================
# 指标
# ============================================================
def ema(a, n):
    a = np.asarray(a, float)
    out = np.full(len(a), np.nan)
    if len(a) == 0:
        return out
    alpha = 2.0 / (n + 1)
    last = np.nan
    for i in range(len(a)):
        v = a[i]
        if np.isnan(v):
            out[i] = last
        else:
            base = last if not np.isnan(last) else v
            out[i] = v * alpha + base * (1 - alpha)
            last = out[i]
    return out


def ffb(x):
    x = np.asarray(x, float).copy()
    last = np.nan
    for i in range(len(x)):
        if not np.isnan(x[i]):
            last = x[i]
        elif not np.isnan(last):
            x[i] = last
    last = np.nan
    for i in range(len(x) - 1, -1, -1):
        if not np.isnan(x[i]):
            last = x[i]
        elif not np.isnan(last):
            x[i] = last
    return x


def rstd(a, n):
    a = np.asarray(a, float)
    out = np.full(len(a), np.nan)
    if len(a) < n:
        return out
    c = np.concatenate([[0.0], np.cumsum(a)])
    c2 = np.concatenate([[0.0], np.cumsum(a * a)])
    s = (c[n:] - c[:-n]) / n
    s2 = (c2[n:] - c2[:-n]) / n
    v = s2 - s * s
    out[n - 1:] = np.sqrt(np.maximum(v, 0.0))
    return out


def atr(h, l, c, n):
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    pc = np.empty_like(c); pc[0] = c[0]; pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.abs(h - pc), np.abs(l - pc))
    return ema(tr, n)


def adx(h, l, c, n=14):
    """Wilder ADX（15m）。返回与输入等长的 adx 序列（0-100 尺度）。

    修复记录：①种子曾用 x[:n].sum()，而 dx 序列前 n-1 位为 NaN（pDI 自 index
    n-1 起有效）→ 种子被 NaN 污染、整条 ADX 全 NaN → 门控静默全拦（空交易）；
    ②种子应为均值而非和，否则 ADX 被放大 ~n 倍。现：ATR/PDM/MDM 用和种子
    （仅进比值，尺度无影响），ADX 从首个无 NaN 的 n 窗口 [n-1, 2n-2] 以均值起算。
    """
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    m = len(c)
    if m < n * 2 + 2:
        return np.full(m, np.nan)
    pc = np.empty_like(c); pc[0] = c[0]; pc[1:] = c[:-1]
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    pDM = np.concatenate([[0.0], np.where((up > dn) & (up > 0), up, 0.0)])
    mDM = np.concatenate([[0.0], np.where((dn > up) & (dn > 0), dn, 0.0)])
    tr = np.maximum(h - l, np.abs(h - pc), np.abs(l - pc))
    a = 1.0 / n

    def wilder_sum(x):
        # x 无 NaN：Wilder 平滑（种子=前 n 项和）
        out = np.full(m, np.nan)
        s = x[:n].sum()
        out[n - 1] = s
        for i in range(n, m):
            s = (1 - a) * s + a * x[i]
            out[i] = s
        return out

    ATR = wilder_sum(tr)
    PDM = wilder_sum(pDM); MDM = wilder_sum(mDM)
    pDI = 100 * PDM / ATR
    mDI = 100 * MDM / ATR
    dx = 100 * np.abs(pDI - mDI) / (pDI + mDI + 1e-9)
    ADX = np.full(m, np.nan)
    i0 = 2 * n - 2
    if m > i0:
        w = dx[i0 - n + 1:i0 + 1]
        if not np.isnan(w).any():
            s = w.mean()                 # ★均值种子（0-100 尺度）
            ADX[i0] = s
            for i in range(i0 + 1, m):
                s = (1 - a) * s + a * dx[i]
                ADX[i] = s
    return ADX


# ============================================================
# 15m 数据加载（含日线方向映射）—— 与 run_meanrev 同格式（缓存通用）
# ============================================================
def load_symbol_15m(sym, engine):
    with engine.connect() as c:
        b15 = pd.read_sql(
            T("SELECT bucket, open, high, low, close FROM l1_mkt.bar_15m "
              "WHERE symbol=:s ORDER BY bucket"),
            c, params={'s': sym})
        db = pd.read_sql(
            T("SELECT trade_date, open, high, low, close FROM l0_raw.daily_bar "
              "WHERE symbol=:s ORDER BY trade_date"),
            c, params={'s': sym})
        segs = load_segments(c, sym, 'min15')
    if len(b15) < 200 or len(db) < 200:
        return None
    dt = pd.to_datetime(b15['bucket'])
    if dt.dt.tz is None:
        dt = dt.dt.tz_localize('Asia/Shanghai')
    else:
        dt = dt.dt.tz_convert('Asia/Shanghai')
    b15['dt'] = dt
    b15['d'] = dt.dt.date
    sig = apply_back_adjust(b15, sym, 'min15', segs=segs)
    o = sig['open'].to_numpy(float)
    h = sig['high'].to_numpy(float)
    l = sig['low'].to_numpy(float)
    cl = sig['close'].to_numpy(float)
    raw_close = b15['close'].to_numpy(float)
    n = len(b15)
    dclose = db['close'].to_numpy(float)
    dema = ema(dclose, 140)
    ddir = np.where(dclose > dema, 1, -1)
    dbd = np.array([pd.Timestamp(x).date() for x in db['trade_date']],
                   dtype='datetime64[D]')
    day = pd.to_datetime(b15['d']).to_numpy(dtype='datetime64[D]')
    pos = np.searchsorted(dbd, day, side='left') - 1
    valid = pos >= 0
    htf_dir = np.zeros(n, dtype=int)
    htf_dir[valid] = ddir[pos[valid]]
    g = b15.groupby('d', sort=True).agg(raw_last=('close', 'last'))
    day_dates = pd.to_datetime(g.index).to_numpy(dtype='datetime64[D]')
    raw_last = g['raw_last'].to_numpy(float)
    sig_last = (pd.Series(cl, index=b15.index)
                .groupby(b15['d']).last().to_numpy(float))
    close_map = (day_dates, raw_last, sig_last)
    sym_cut = (pd.Timestamp(b15['d'].iloc[0]) + pd.Timedelta(days=200)).date()
    return dict(sym=sym, o=o, h=h, l=l, c=cl, raw_close=raw_close,
                dt=b15['dt'].to_numpy(dtype='datetime64[ns]'), days=day,
                htf_dir=htf_dir, close_map=close_map, sym_cut=sym_cut)


# ============================================================
# 事件构造（15m）
# ============================================================
def mk_ev_15(sd, i, kind, dirn, px, raw, rank, score=0.0):
    return dict(dt=pd.Timestamp(sd['dt'][i]), rank=rank, sym=sd['sym'],
                kind=kind, dir=dirn, px_raw=raw, px_adj=px, score=score,
                d=sd['days'][i], _close_map=sd['close_map'])


# ============================================================
# 均值回归信号生成器（扩展：entry_k 加深 + ADX 门控）
#   n / entry_k / stop_k / use_trend / intraday 同前
#   adx_max：ADX(15m) 上限，<=该值才允许均值回归入场；0=不限制
# ============================================================
def gen_meanrev(sd, p):
    c = ffb(sd['c']); h = ffb(sd['h']); l = ffb(sd['l'])
    raw = ffb(sd['raw_close'])
    n = len(c); mid = ema(c, p['n']); band = rstd(c, p['n'])
    ddir_b = sd['htf_dir']
    days = sd['days']
    cut = np.datetime64(sd['sym_cut'])
    ek = p['entry_k']; sk = p['stop_k']
    intraday = p.get('intraday', 0)
    adx_max = p.get('adx_max', 0)
    adx_v = sd.get('adx15')
    ev = []; pos = 0
    for i in range(p['n'], n):
        if days[i] < cut:
            continue
        b = band[i]
        if b <= 0 or np.isnan(b):
            continue
        z = (c[i] - mid[i]) / b
        dd = ddir_b[i]
        last_of_day = (i + 1 >= n) or (days[i + 1] != days[i])
        if pos == 0:
            if adx_max and (adx_v is None or np.isnan(adx_v[i]) or adx_v[i] > adx_max):
                continue
            if (p['use_trend'] == 0 or dd >= 0) and z <= -ek:
                ev.append(mk_ev_15(sd, i, 'ENTRY', 1, c[i], raw[i], 2, -z))
                pos = 1
            elif (p['use_trend'] == 0 or dd <= 0) and z >= ek:
                ev.append(mk_ev_15(sd, i, 'ENTRY', -1, c[i], raw[i], 2, z))
                pos = -1
        else:
            if pos == 1:
                if z >= 0 or z <= -sk or (intraday and last_of_day):
                    ev.append(mk_ev_15(sd, i, 'EXIT', 1, c[i], raw[i], 0))
                    pos = 0
            else:
                if z <= 0 or z >= sk or (intraday and last_of_day):
                    ev.append(mk_ev_15(sd, i, 'EXIT', -1, c[i], raw[i], 0))
                    pos = 0
    return ev


# ============================================================
# 参数网格（新方向）
# ============================================================
STRAT = dict(gen=gen_meanrev, max_hold=12, add_max=0, cap=20,
             grid=[dict(n=nn, entry_k=ek, stop_k=sk, use_trend=1, intraday=it, adx_max=am)
                   for nn in (50, 100)
                   for ek in (2.5, 3.0)
                   for sk in (4.0,)
                   for it in (0, 1)
                   for am in (0, 20, 25)])


# ============================================================
# simulate（复制自 run_meanrev，通用）
# ============================================================
def simulate(all_events, days_global, margin=None, cap=True,
             max_products=20, max_hold=30, add_max_lots=0):
    ev_sorted = sorted(all_events,
                       key=lambda e: (e['dt'], e['rank'], -e.get('score', 0.0)))
    day_idx = {d: i for i, d in enumerate(days_global)}
    n_days = len(days_global)
    positions = {}
    trades = []
    blocked = timeouts = 0
    max_conc = 0
    total_lots = 0
    cur_margin = cur_notional = 0.0
    peak_margin = peak_notional = 0.0
    peak_margin_day = None
    total_lots_cap = max_products * 2

    def day_pos(dq, close_map):
        dates, raws, sigs = close_map
        p = int(np.searchsorted(dates, np.datetime64(dq), side='right')) - 1
        if p < 0:
            return None
        return float(raws[p]), float(sigs[p])

    def lot_margin(sym, px_raw, dirn):
        if margin is None:
            return 0.0
        f = margin.get(sym)
        return 0.0 if f is None else px_raw * (f[0] if dirn == 1 else f[1])

    def ii2date(ii):
        return days_global[ii]

    def close_all(sym, dq, reason, price):
        nonlocal total_lots, cur_margin, cur_notional
        pos = positions.pop(sym)
        total_lots -= len(pos['lots'])
        ii = day_idx[dq]
        raw_e, sig_e = price
        for li, lot in enumerate(pos['lots']):
            c_open, mult = CB.half(sym, lot['d'], lot['px_raw'])
            c_close, _ = CB.half(sym, ii2date(ii), raw_e)
            dir_sgn = 1.0 if pos['dir'] == 1 else -1.0
            pts = (sig_e - lot['px_adj']) * dir_sgn
            pnl = pts * mult - c_open - c_close
            trades.append(dict(symbol=sym,
                               side='LONG' if pos['dir'] == 1 else 'SHORT',
                               entry_date=str(lot['d']), exit_date=str(ii2date(ii)),
                               lot_kind='base' if li == 0 else 'addon', lots=1,
                               pnl=pnl, cost=c_open + c_close, mult=mult,
                               hold_days=ii - lot['entry_i'], exit_reason=reason,
                               eyear=pd.Timestamp(lot['d']).year,
                               margin=lot.get('margin', 0.0)))
            cur_margin -= lot.get('margin', 0.0)
            cur_notional -= lot['px_raw'] * mult

    i_checked = -1
    k = 0
    ne = len(ev_sorted)
    while k < ne:
        dt0 = ev_sorted[k]['dt']
        dq = np.datetime64(dt0.date() if hasattr(dt0, 'date') else dt0)
        di = day_idx.get(dq)
        if di is None:
            k += 1
            continue
        for ii in range(i_checked + 1, di + 1):
            dchk = days_global[ii]
            for sym in list(positions):
                pos = positions[sym]
                if ii - pos['lots'][0]['entry_i'] >= max_hold:
                    pr = day_pos(dchk, pos['close_map'])
                    if pr is None:
                        pr = (pos['lots'][-1]['px_raw'], pos['lots'][-1]['px_adj'])
                    close_all(sym, dchk, 'TIMEOUT', pr)
                    timeouts += 1
        i_checked = di
        while k < ne and ev_sorted[k]['dt'] == dt0:
            e = ev_sorted[k]
            k += 1
            sym = e['sym']
            if e['kind'] == 'EXIT':
                if sym in positions:
                    close_all(sym, e['d'], 'SIGNAL', (e['px_raw'], e['px_adj']))
            elif e['kind'] == 'ENTRY':
                if sym in positions:
                    continue
                if cap and (len(positions) >= max_products
                            or total_lots >= total_lots_cap):
                    blocked += 1
                    continue
                m = lot_margin(sym, e['px_raw'], e['dir'])
                mult = CB.coef(sym, e['d'])['multiplier']
                positions[sym] = dict(
                    dir=e['dir'], score=e.get('score', 0.0),
                    close_map=e['_close_map'],
                    lots=[dict(d=e['d'], px_raw=e['px_raw'], px_adj=e['px_adj'],
                               entry_i=day_idx[e['d']], margin=m, li=0)])
                total_lots += 1
                cur_margin += m
                cur_notional += e['px_raw'] * mult
                if cur_margin > peak_margin:
                    peak_margin, peak_margin_day = cur_margin, str(e['d'])
                peak_notional = max(peak_notional, cur_notional)
                max_conc = max(max_conc, len(positions))
    for ii in range(i_checked + 1, n_days):
        dchk = days_global[ii]
        for sym in list(positions):
            pos = positions[sym]
            if ii - pos['lots'][0]['entry_i'] >= max_hold:
                pr = day_pos(dchk, pos['close_map'])
                if pr is None:
                    pr = (pos['lots'][-1]['px_raw'], pos['lots'][-1]['px_adj'])
                close_all(sym, dchk, 'TIMEOUT', pr)
                timeouts += 1
    last_d = days_global[-1]
    for sym in list(positions):
        pos = positions[sym]
        pr = day_pos(last_d, pos['close_map'])
        if pr is None:
            pr = (pos['lots'][-1]['px_raw'], pos['lots'][-1]['px_adj'])
        close_all(sym, last_d, 'FORCE_END', pr)
    return dict(trades=trades, blocked=blocked, timeouts=timeouts,
                max_concurrent=max_conc, peak_margin=round(peak_margin, 0),
                peak_notional=round(peak_notional, 0), peak_margin_day=peak_margin_day)


def equity_mdd(df):
    if not len(df):
        return 0.0
    s = df.groupby('exit_date').pnl.sum().sort_index()
    cum = s.cumsum()
    return float((cum - cum.cummax()).min())


def summarize(trades, label, max_hold):
    if not trades:
        return dict(label=label, n=0, net=0.0)
    df = pd.DataFrame(trades)
    net = float(df.pnl.sum())
    wins = df[df.pnl > 0]; losses = df[df.pnl <= 0]
    gross_w = float(wins.pnl.sum()); gross_l = float(abs(losses.pnl.sum()))
    pf = gross_w / gross_l if gross_l > 0 else float('inf')
    mdd = equity_mdd(df)
    by_year = {str(y): round(float(g.pnl.sum()), 0)
               for y, g in df.groupby('eyear')}
    by_sym = df.groupby('symbol').pnl.sum().sort_values()
    return dict(label=label, n=int(len(df)), net=round(net, 0),
                win=round(100 * len(wins) / len(df), 1),
                pf=round(pf, 3) if pf != float('inf') else 999.0,
                cost=round(float(df.cost.sum()), 0),
                per_trade=round(net / len(df), 0),
                n_syms=int(df.symbol.nunique()),
                mdd=round(mdd, 0),
                suggest_capital=round(0.0, 0),
                by_year=by_year,
                top5=[dict(s=s, v=round(float(v), 0)) for s, v in
                      by_sym.sort_values(ascending=False).head(5).items()],
                worst5=[dict(s=s, v=round(float(v), 0)) for s, v in by_sym.head(5).items()])


def run_is(data, days_global, margin):
    gen = STRAT['gen']; grid = STRAT['grid']; mh = STRAT['max_hold']
    per_combo = {}
    for p in grid:
        t0 = time.time()
        per_prod = {}
        for sym, sd in data.items():
            ev = gen(sd, p)
            ev = [e for e in ev if e['d'] < CUT]
            if not ev:
                continue
            sim = simulate(ev, days_global[days_global < CUT], margin=margin,
                           cap=False, max_products=9999, max_hold=mh, add_max_lots=0)
            df = pd.DataFrame(sim['trades'])
            net = float(df.pnl.sum()) if len(df) else 0.0
            ntr = len(df)
            if net != 0.0 or ntr:
                per_prod[sym] = (net, ntr)
        total = sum(v[0] for v in per_prod.values())
        per_combo[(tuple(sorted(p.items())))] = dict(
            p=p, total=round(total, 0), per_prod=per_prod, n_prod=len(per_prod))
        print('[IS] %s total=%.0f n_prod=%d (%.0fs)' % (
            p, total, len(per_prod), time.time() - t0), flush=True)
    best_k = max(per_combo, key=lambda k: per_combo[k]['total'])
    best = per_combo[best_k]
    sel = sorted([s for s, (net, ntr) in best['per_prod'].items()
                  if net > 0 and ntr >= 5])
    is_res = dict(best_p=best['p'], best_total=best['total'],
                  selected=sel, n_selected=len(sel),
                  combos={('%s' % (k,)): dict(total=v['total'], n_prod=v['n_prod'])
                          for k, v in per_combo.items()})
    print('[IS] BEST %s total=%.0f selected=%d' % (
        best['p'], best['total'], len(sel)), flush=True)
    return is_res


def run_oos(data, days_global, margin, is_res):
    gen = STRAT['gen']; mh = STRAT['max_hold']; am = STRAT['add_max']; cap = STRAT['cap']
    p = is_res['best_p']
    selected = set(is_res['selected'])
    t0 = time.time()
    ev_sel = []
    for sym, sd in data.items():
        if sym not in selected:
            continue
        for e in gen(sd, p):
            e['_close_map'] = sd['close_map']
            if e['d'] >= CUT:
                ev_sel.append(e)
    sim_sel = simulate(ev_sel, days_global[days_global >= CUT], margin=margin,
                       cap=True, max_products=cap, max_hold=mh, add_max_lots=am)
    m_sel = summarize(sim_sel['trades'], 'mr2_sel', mh)
    m_sel['peak_margin'] = sim_sel['peak_margin']
    m_sel['max_conc'] = sim_sel['max_concurrent']
    m_sel['suggest_capital'] = round(sim_sel['peak_margin'] + abs(m_sel['mdd']), 0)
    m_sel['blocked'] = sim_sel['blocked']; m_sel['timeouts'] = sim_sel['timeouts']
    pd.DataFrame(sim_sel['trades']).to_csv(
        os.path.join(OUT, 'oos_trades_sel.csv'), index=False, encoding='utf-8-sig')
    ev_all = []
    for sym, sd in data.items():
        for e in gen(sd, p):
            e['_close_map'] = sd['close_map']
            if e['d'] >= CUT:
                ev_all.append(e)
    sim_all = simulate(ev_all, days_global[days_global >= CUT], margin=margin,
                       cap=True, max_products=cap, max_hold=mh, add_max_lots=am)
    m_all = summarize(sim_all['trades'], 'mr2_all', mh)
    m_all['peak_margin'] = sim_all['peak_margin']
    m_all['max_conc'] = sim_all['max_concurrent']
    m_all['suggest_capital'] = round(sim_all['peak_margin'] + abs(m_all['mdd']), 0)
    m_all['blocked'] = sim_all['blocked']; m_all['timeouts'] = sim_all['timeouts']
    pd.DataFrame(sim_all['trades']).to_csv(
        os.path.join(OUT, 'oos_trades_all.csv'), index=False, encoding='utf-8-sig')
    oos = dict(p=p, selected=is_res['n_selected'], sel=m_sel, all=m_all)
    print('[OOS] p=%s sel net=%.0f pf=%.3f win=%.1f%% n=%d MDD=%.0f 建议%.0f '
          '| all net=%.0f (%.0fs)' % (
              p, m_sel['net'], m_sel['pf'], m_sel['win'], m_sel['n'],
              m_sel['mdd'], m_sel['suggest_capital'], m_all['net'],
              time.time() - t0), flush=True)
    return oos


def main():
    V._isolate_cost_connections()
    engine = V.get_engine()
    t0 = time.time()
    universe, skipped = V.build_universe(engine)
    if SMOKE:
        universe = universe[:5]
    print('universe=%d skipped=%d' % (len(universe), len(skipped)), flush=True)

    data = None
    if os.path.exists(CACHE) and not SMOKE:
        try:
            with open(CACHE, 'rb') as f:
                data = pickle.load(f)
            if set(data.keys()) != set(universe):
                print('cache stale -> rebuild', flush=True)
                data = None
            else:
                print('cache loaded %d syms (%.0fs)' % (len(data), time.time() - t0), flush=True)
        except Exception as ex:
            print('cache err %s' % str(ex)[:100], flush=True)
            data = None
    if data is None:
        data = {}
        for i, sym in enumerate(universe):
            try:
                sd = load_symbol_15m(sym, engine)
            except Exception as ex:
                print('LOAD-ERR %s: %s' % (sym, str(ex)[:120]), flush=True)
                sd = None
            if sd is not None:
                data[sym] = sd
            if (i + 1) % 10 == 0:
                print('loaded %d/%d (%.0fs)' % (i + 1, len(universe), time.time() - t0), flush=True)
        if not SMOKE:
            with open(CACHE, 'wb') as f:
                pickle.dump(data, f)
        print('cache written %d syms (%.0fs)' % (len(data), time.time() - t0), flush=True)

    # 预计算 15m ADX（供 ADX 门控）。复用缓存键，避免重复计算。
    for sym, sd in data.items():
        sd['adx15'] = adx(sd['h'], sd['l'], sd['c'], 14)

    margin, msrc = V.load_margin_map(engine, universe)
    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    print('days=%d (%s -> %s) OOS_days=%d' % (
        len(days_global), days_global[0], days_global[-1],
        int((days_global >= CUT).sum())), flush=True)

    is_res = None
    if not OOS_ONLY:
        is_res = run_is(data, days_global, margin)
        with open('/tmp/mr2_is.pkl', 'wb') as f:
            pickle.dump(is_res, f)
    if not IS_ONLY:
        if is_res is None:
            with open('/tmp/mr2_is.pkl', 'rb') as f:
                is_res = pickle.load(f)
        oos = run_oos(data, days_global, margin, is_res)
        with open(os.path.join(OUT, 'mr2_results.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(cut=str(CUT), n_syms=len(data),
                           oos_days=int((days_global >= CUT).sum()),
                           is_res=dict(best_p=is_res['best_p'],
                                       best_total=is_res['best_total'],
                                       n_selected=is_res['n_selected'],
                                       combos=is_res['combos']),
                           oos=dict(p=oos['p'], selected=oos['selected'],
                                    sel={k: oos['sel'][k] for k in
                                         ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                          'suggest_capital', 'peak_margin', 'max_conc',
                                          'n_syms', 'by_year', 'top5', 'worst5',
                                          'blocked', 'timeouts', 'per_trade')},
                                    all={k: oos['all'][k] for k in
                                         ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                          'suggest_capital', 'peak_margin', 'max_conc',
                                          'n_syms', 'by_year', 'blocked', 'timeouts')})),
                      f, ensure_ascii=False, indent=1, default=str)
        print('DONE %.0fs' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
