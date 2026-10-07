# -*- coding: utf-8 -*-
"""四策略 walk-forward 回测（2015-2020 选品+参数，2021-2026 纯样本外）。

复用 run_param_scan_v3 的：数据缓存 / CostBook / 保证金 / 品种池 / simulate 结构。
本模块只新增 4 个信号生成器 + 一个支持 REDUCE（网格部分平仓）的 simulate + 指标汇总。

事件 schema（与 V.simulate 一致）：
  ENTRY  {dir=±1, px_raw, px_adj, score, d, _close_map}  开 1 手底仓
  ADD    {dir, px_raw, px_adj, n_add, d, _close_map}      加仓（金字塔）
  REDUCE {px_raw, px_adj, n_reduce, d, _close_map}        部分平仓（网格用）
  EXIT   {px_raw, px_adj, d, _close_map}                  平全部
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

import run_param_scan_v3 as V
from types import SimpleNamespace

OUT = '/tmp/four_out'
os.makedirs(OUT, exist_ok=True)
CACHE = V.CACHE
CUT = np.datetime64('2021-01-01')          # IS = 2015-2020，OOS = 2021-2026
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
    """前向填充 + 后向填充 NaN（处理上市初期个别缺失 bar）。"""
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


def sma(a, n):
    a = np.asarray(a, float)
    out = np.full(len(a), np.nan)
    if len(a) < n:
        return out
    c = np.concatenate([[0.0], np.cumsum(a)])
    s = (c[n:] - c[:-n]) / n
    out[n - 1:] = s
    return out


def rstd(a, n):
    a = np.asarray(a, float)
    m = sma(a, n)
    v = sma(a * a, n) - m * m
    return np.sqrt(np.maximum(v, 0.0))


def atr(h, l, c, n):
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    pc = np.empty_like(c); pc[0] = c[0]; pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.abs(h - pc), np.abs(l - pc))
    return ema(tr, n)


def roll_max(a, n):
    a = np.asarray(a, float); out = np.full(len(a), np.nan)
    for i in range(n, len(a) + 1):
        out[i - 1] = a[i - n:i].max()
    return out


def roll_min(a, n):
    a = np.asarray(a, float); out = np.full(len(a), np.nan)
    for i in range(n, len(a) + 1):
        out[i - 1] = a[i - n:i].min()
    return out


def daily_ohlc(sd):
    days = sd['days']; c = sd['c']; h = sd['h']; l = sd['l']; o = sd['o']; raw = sd['raw_close']
    df = pd.DataFrame({'day': days, 'o': o, 'h': h, 'l': l, 'c': c, 'r': raw})
    g = df.groupby('day', sort=True).agg({'o': 'first', 'h': 'max', 'l': 'min',
                                          'c': 'last', 'r': 'last'})
    return g


# ============================================================
# 事件构造
# ============================================================
def mk_ev_60(sd, i, kind, dirn, px, raw, rank, score=0.0, n=1):
    return dict(dt=sd['dts'][i], rank=rank, sym=sd['sym'], kind=kind, dir=dirn,
                px_raw=raw, px_adj=px, score=score, d=sd['days'][i],
                _close_map=sd['close_map'], n_add=n, n_reduce=n)


def mk_ev_day(sym, day, px, raw, kind, dirn, close_map, rank, score=0.0, n=1):
    dt = pd.Timestamp(day) + pd.Timedelta(hours=15)
    return dict(dt=dt, rank=rank, sym=sym,
                kind=kind, dir=dirn, px_raw=raw, px_adj=px, score=score,
                d=np.datetime64(day, 'D'),
                _close_map=close_map, n_add=n, n_reduce=n)


# ============================================================
# 策略 1：波段（趋势回撤 / 快线穿越）
#   f=快线周期；use_trend=是否用日线 EMA140 方向过滤；sl_atr=ATR 止损倍数
# ============================================================
def gen_swing(sd, p):
    c = ffb(sd['c']); h = ffb(sd['h']); l = ffb(sd['l']); days = sd['days']
    raw = ffb(sd['raw_close'])
    n = len(c); fast = ema(c, p['f']); atr_v = atr(h, l, c, 14)
    trend = sd['htf_dir'] if p['use_trend'] else np.ones(n, dtype=int)
    cut = np.datetime64(sd['sym_cut'])
    ev = []; pos = 0; prev_c = np.nan; prev_f = np.nan; entry_px = 0.0
    for i in range(1, n):
        if days[i] < cut:
            prev_c = c[i]; prev_f = fast[i]; continue
        cx = c[i]; fx = fast[i]; tr = trend[i]; a = atr_v[i]
        if pos == 0:
            if (p['use_trend'] == 0 or tr >= 0) and prev_c <= prev_f and cx > fx:
                sc = (cx - fx) / a if a > 0 else 0.0
                ev.append(mk_ev_60(sd, i, 'ENTRY', 1, cx, raw[i], 2, sc))
                pos = 1; entry_px = cx
            elif (p['use_trend'] == 0 or tr <= 0) and prev_c >= prev_f and cx < fx:
                sc = (fx - cx) / a if a > 0 else 0.0
                ev.append(mk_ev_60(sd, i, 'ENTRY', -1, cx, raw[i], 2, sc))
                pos = -1; entry_px = cx
        else:
            if pos == 1:
                stop = entry_px - p['sl_atr'] * a
                if cx < fx or cx < stop or (p['use_trend'] and tr < 0):
                    ev.append(mk_ev_60(sd, i, 'EXIT', 1, cx, raw[i], 0))
                    pos = 0
            else:
                stop = entry_px + p['sl_atr'] * a
                if cx > fx or cx > stop or (p['use_trend'] and tr > 0):
                    ev.append(mk_ev_60(sd, i, 'EXIT', -1, cx, raw[i], 0))
                    pos = 0
        prev_c = cx; prev_f = fx
    return ev


# ============================================================
# 策略 2：海龟（日线唐奇安突破 + N 金字塔 + 2N 止损）
#   entry_ch / exit_ch / atr_n / use_ema(200日过滤)
# ============================================================
def gen_turtle(sd, p):
    g = daily_ohlc(sd)
    H = ffb(g['h'].values.astype(float)); L = ffb(g['l'].values.astype(float))
    C = ffb(g['c'].values.astype(float)); R = ffb(g['r'].values.astype(float))
    days = g.index.values
    nd = len(H)
    if nd < 250:
        return []
    atr_v = atr(H, L, C, p['atr_n'])
    he = roll_max(H, p['entry_ch'])      # 入场通道上轨（含自身？用 prior：下面检测用 H[i]>=he[i-1]）
    le = roll_min(L, p['entry_ch'])
    hx = roll_max(H, p['exit_ch'])
    lx = roll_min(L, p['exit_ch'])
    if p['use_ema']:
        ema200 = ema(C, 200)
    else:
        ema200 = np.full(nd, np.nan)
    cut = np.datetime64(sd['sym_cut'])
    cm = sd['close_map']
    ev = []; pos = 0; units = 0; entry_px = 0.0; last_add = 0.0
    for i in range(max(p['entry_ch'], p['exit_ch']), nd):
        day = days[i]
        if day < cut:
            continue
        a = atr_v[i - 1] if i > 0 and not np.isnan(atr_v[i - 1]) else atr_v[i]
        if np.isnan(a) or a <= 0:
            continue
        # 离场优先
        if pos == 1:
            stop = last_add - 2.0 * a
            if L[i] <= lx[i - 1] or L[i] <= stop:
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'EXIT', 1, cm, 0))
                pos = 0; units = 0
            elif (not np.isnan(ema200[i])) and C[i] < ema200[i]:
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'EXIT', 1, cm, 0))
                pos = 0; units = 0
        elif pos == -1:
            stop = last_add + 2.0 * a
            if H[i] >= hx[i - 1] or H[i] >= stop:
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'EXIT', -1, cm, 0))
                pos = 0; units = 0
            elif (not np.isnan(ema200[i])) and C[i] > ema200[i]:
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'EXIT', -1, cm, 0))
                pos = 0; units = 0
        if pos == 0:
            # 突破入场
            if np.isnan(he[i - 1]) or np.isnan(le[i - 1]):
                continue
            if H[i] >= he[i - 1] and (np.isnan(ema200[i]) or C[i] > ema200[i]):
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'ENTRY', 1, cm, 2,
                                    (H[i] - he[i - 1]) / a))
                pos = 1; units = 1; entry_px = C[i]; last_add = C[i]
            elif L[i] <= le[i - 1] and (np.isnan(ema200[i]) or C[i] < ema200[i]):
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'ENTRY', -1, cm, 2,
                                    (le[i - 1] - L[i]) / a))
                pos = -1; units = 1; entry_px = C[i]; last_add = C[i]
        else:
            # 金字塔加仓
            if pos == 1 and units < 4 and C[i] >= last_add + 0.5 * a:
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'ADD', 1, cm, 1,
                                    (C[i] - last_add) / a, n=1))
                units += 1; last_add = C[i]
            elif pos == -1 and units < 4 and C[i] <= last_add - 0.5 * a:
                ev.append(mk_ev_day(sd['sym'], day, C[i], R[i], 'ADD', -1, cm, 1,
                                    (last_add - C[i]) / a, n=1))
                units += 1; last_add = C[i]
    return ev


# ============================================================
# 策略 3：网格（EMA 距离目标仓位，多头网格 + 趋势破位保护）
#   slow=中心 EMA 周期；gap=间距(ATR 倍数)；levels=最大手数
# ============================================================
def gen_grid(sd, p):
    c = ffb(sd['c']); h = ffb(sd['h']); l = ffb(sd['l']); days = sd['days']
    raw = ffb(sd['raw_close'])
    n = len(c); slow = ema(c, p['slow']); atr_v = atr(h, l, c, 14)
    cut = np.datetime64(sd['sym_cut'])
    ev = []; units = 0
    for i in range(1, n):
        if days[i] < cut:
            continue
        a = atr_v[i]
        if np.isnan(a) or a <= 0:
            continue
        cx = c[i]; cen = slow[i]; sp = p['gap'] * a
        lvl = int(np.clip(round((cen - cx) / sp), 0, p['levels']))
        if lvl > units:
            add = lvl - units
            if units == 0:
                ev.append(mk_ev_60(sd, i, 'ENTRY', 1, cx, raw[i], 2, (cen - cx) / sp))
                if add > 1:
                    ev.append(mk_ev_60(sd, i, 'ADD', 1, cx, raw[i], 1, 0, n=add - 1))
            else:
                ev.append(mk_ev_60(sd, i, 'ADD', 1, cx, raw[i], 1, 0, n=add))
            units = lvl
        elif lvl < units:
            red = units - lvl
            if lvl == 0:
                ev.append(mk_ev_60(sd, i, 'EXIT', 1, cx, raw[i], 0))
            else:
                ev.append(mk_ev_60(sd, i, 'REDUCE', 1, cx, raw[i], 1, 0, n=red))
            units = lvl
        # 趋势破位保护：远离中心超过 (levels+1) 个间距 → 平全部
        if units > 0 and abs(cx - cen) > (p['levels'] + 1) * sp:
            ev.append(mk_ev_60(sd, i, 'EXIT', 1, cx, raw[i], 0))
            units = 0
    return ev


# ============================================================
# 策略 4：隐秘数轴（日线枢轴价位反转）
#   lb=枢轴回望天数；use_r2=是否用 R2/S2；sl_atr=ATR 止损倍数
# ============================================================
def gen_hidden(sd, p):
    g = daily_ohlc(sd)
    H = ffb(g['h'].values.astype(float)); L = ffb(g['l'].values.astype(float))
    C = ffb(g['c'].values.astype(float)); R = ffb(g['r'].values.astype(float))
    days = g.index.values
    nd = len(H)
    if nd < 60:
        return []
    atr_v = atr(H, L, C, 14)
    cut = np.datetime64(sd['sym_cut'])
    cm = sd['close_map']
    ev = []; pos = 0; entry_px = 0.0
    lb = p['lb']
    for i in range(lb + 1, nd):
        day = days[i]
        if day < cut:
            continue
        a = atr_v[i - 1] if not np.isnan(atr_v[i - 1]) else atr_v[i]
        if np.isnan(a) or a <= 0:
            continue
        Hi = H[i - lb:i].max(); Li = L[i - lb:i].min(); Cc = C[i - 1]
        P = (Hi + Li + Cc) / 3.0
        R1 = 2 * P - Li; S1 = 2 * P - Hi
        R2 = P + (Hi - Li); S2 = P - (Hi - Li)
        cx = C[i]; hi = H[i]; lo = L[i]
        if pos == 0:
            # 触及支撑反弹 → 多；触及阻力回落 → 空
            if lo <= S1 and cx > S1:
                ev.append(mk_ev_day(sd['sym'], day, cx, R[i], 'ENTRY', 1, cm, 2, (S1 - lo) / a))
                pos = 1; entry_px = cx
            elif p['use_r2'] and lo <= S2 and cx > S2:
                ev.append(mk_ev_day(sd['sym'], day, cx, R[i], 'ENTRY', 1, cm, 2, (S2 - lo) / a))
                pos = 1; entry_px = cx
            elif hi >= R1 and cx < R1:
                ev.append(mk_ev_day(sd['sym'], day, cx, R[i], 'ENTRY', -1, cm, 2, (hi - R1) / a))
                pos = -1; entry_px = cx
            elif p['use_r2'] and hi >= R2 and cx < R2:
                ev.append(mk_ev_day(sd['sym'], day, cx, R[i], 'ENTRY', -1, cm, 2, (hi - R2) / a))
                pos = -1; entry_px = cx
        else:
            if pos == 1:
                stop = entry_px - p['sl_atr'] * a
                if hi >= P or cx < stop:
                    ev.append(mk_ev_day(sd['sym'], day, cx, R[i], 'EXIT', 1, cm, 0))
                    pos = 0
            else:
                stop = entry_px + p['sl_atr'] * a
                if lo <= P or cx > stop:
                    ev.append(mk_ev_day(sd['sym'], day, cx, R[i], 'EXIT', -1, cm, 0))
                    pos = 0
    return ev


# ============================================================
# 参数网格
# ============================================================
STRATS = {
    'swing': dict(gen=gen_swing, max_hold=20, add_max=0, cap=20,
                  grid=[dict(f=f, use_trend=ut, sl_atr=sl)
                        for f in (20, 40) for ut in (0, 1) for sl in (2.5, 3.5)]),
    'turtle': dict(gen=gen_turtle, max_hold=200, add_max=4, cap=20,
                   grid=[dict(entry_ch=ec, exit_ch=xc, atr_n=20, use_ema=ue)
                         for ec in (20, 55) for xc in (10, 20) for ue in (0, 1)]),
    'grid': dict(gen=gen_grid, max_hold=60, add_max=20, cap=40,
                 grid=[dict(slow=s, gap=g, levels=6)
                       for s in (60, 120, 240) for g in (1.0, 2.0)]),
    'hidden': dict(gen=gen_hidden, max_hold=15, add_max=0, cap=20,
                   grid=[dict(lb=lb, use_r2=r2, sl_atr=sl)
                         for lb in (1, 2) for r2 in (1,) for sl in (2.0, 3.0)]),
}


# ============================================================
# 扩展 simulate（支持 REDUCE 部分平仓）
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

    def close_some(sym, dq, reason, price, k):
        nonlocal total_lots, cur_margin, cur_notional
        pos = positions[sym]
        ii = day_idx[dq]
        raw_e, sig_e = price
        for _ in range(k):
            if not pos['lots']:
                break
            lot = pos['lots'].pop(0)
            c_open, mult = CB.half(sym, lot['d'], lot['px_raw'])
            c_close, _ = CB.half(sym, ii2date(ii), raw_e)
            dir_sgn = 1.0 if pos['dir'] == 1 else -1.0
            pts = (sig_e - lot['px_adj']) * dir_sgn
            pnl = pts * mult - c_open - c_close
            trades.append(dict(symbol=sym,
                               side='LONG' if pos['dir'] == 1 else 'SHORT',
                               entry_date=str(lot['d']), exit_date=str(ii2date(ii)),
                               lot_kind='base' if lot['li'] == 0 else 'addon', lots=1,
                               pnl=pnl, cost=c_open + c_close, mult=mult,
                               hold_days=ii - lot['entry_i'], exit_reason=reason,
                               eyear=pd.Timestamp(lot['d']).year,
                               margin=lot.get('margin', 0.0)))
            cur_margin -= lot.get('margin', 0.0)
            cur_notional -= lot['px_raw'] * mult
        total_lots -= k

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
            elif e['kind'] == 'REDUCE':
                pos = positions.get(sym)
                if pos is None:
                    continue
                kk = min(int(e.get('n_reduce', 1)), len(pos['lots']))
                if kk <= 0:
                    continue
                close_some(sym, e['d'], 'GRID', (e['px_raw'], e['px_adj']), kk)
                if not pos['lots']:
                    positions.pop(sym)
            elif e['kind'] == 'ADD':
                pos = positions.get(sym)
                if pos is None or len(pos['lots']) >= add_max_lots + 1:
                    continue
                if cap and total_lots >= total_lots_cap:
                    continue
                nadd = int(e.get('n_add', 1))
                m = lot_margin(sym, e['px_raw'], pos['dir'])
                mult = CB.coef(sym, e['d'])['multiplier']
                for _ in range(nadd):
                    if len(pos['lots']) >= add_max_lots + 1:
                        break
                    pos['lots'].append(dict(d=e['d'], px_raw=e['px_raw'],
                                            px_adj=e['px_adj'],
                                            entry_i=day_idx[e['d']], margin=m,
                                            li=len(pos['lots'])))
                    total_lots += 1
                    cur_margin += m
                    cur_notional += e['px_raw'] * mult
                if cur_margin > peak_margin:
                    peak_margin, peak_margin_day = cur_margin, str(e['d'])
                peak_notional = max(peak_notional, cur_notional)
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


# ============================================================
# 指标汇总
# ============================================================
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
                suggest_capital=round(0.0, 0),  # 在调用处补
                by_year=by_year,
                top5=[dict(s=s, v=round(float(v), 0)) for s, v in
                      by_sym.sort_values(ascending=False).head(5).items()],
                worst5=[dict(s=s, v=round(float(v), 0)) for s, v in by_sym.head(5).items()])


# ============================================================
# IS 扫描
# ============================================================
def run_is(data, days_global, margin):
    is_res = {}
    for sname, sc in STRATS.items():
        gen = sc['gen']; grid = sc['grid']; mh = sc['max_hold']
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
                               cap=False, max_products=9999, max_hold=mh,
                               add_max_lots=sc['add_max'])
                net = float(pd.DataFrame(sim['trades']).pnl.sum()) if sim['trades'] else 0.0
                ntr = len(sim['trades'])
                if net != 0.0 or ntr:
                    per_prod[sym] = (net, ntr)
            total = sum(v[0] for v in per_prod.values())
            per_combo[(sname, tuple(sorted(p.items())))] = dict(
                p=p, total=round(total, 0), per_prod=per_prod,
                n_prod=len(per_prod))
            print('[IS][%s] %s total=%.0f n_prod=%d (%.0fs)' % (
                sname, p, total, len(per_prod), time.time() - t0), flush=True)
        # 选最优参数（IS 总净最高）
        best_k = max(per_combo, key=lambda k: per_combo[k]['total'])
        best = per_combo[best_k]
        # 选品：IS 净>0 且 IS 成交≥5 笔（排除样本过小噪声）
        sel = sorted([s for s, (net, ntr) in best['per_prod'].items()
                      if net > 0 and ntr >= 5])
        is_res[sname] = dict(best_p=best['p'], best_total=best['total'],
                             selected=sel, n_selected=len(sel),
                             combos={('%s' % (k[1],)): dict(total=v['total'],
                                        n_prod=v['n_prod']) for k, v in per_combo.items()})
        print('[IS][%s] BEST %s total=%.0f selected=%d' % (
            sname, best['p'], best['total'], len(sel)), flush=True)
    return is_res


# ============================================================
# OOS 跑批
# ============================================================
def run_oos(data, days_global, margin, is_res):
    oos_res = {}
    days_oos = days_global[days_global >= CUT]
    for sname, sc in STRATS.items():
        gen = sc['gen']; mh = sc['max_hold']; am = sc['add_max']; cap = sc['cap']
        p = is_res[sname]['best_p']
        selected = set(is_res[sname]['selected'])
        t0 = time.time()
        # 选品版
        ev_sel = []
        for sym, sd in data.items():
            if sym not in selected:
                continue
            for e in gen(sd, p):
                e['_close_map'] = sd['close_map']
                if e['d'] >= CUT:
                    ev_sel.append(e)
        sim_sel = simulate(ev_sel, days_oos, margin=margin, cap=True,
                           max_products=cap, max_hold=mh, add_max_lots=am)
        m_sel = summarize(sim_sel['trades'], '%s_sel' % sname, mh)
        m_sel['peak_margin'] = sim_sel['peak_margin']
        m_sel['max_conc'] = sim_sel['max_concurrent']
        m_sel['suggest_capital'] = round(sim_sel['peak_margin'] + abs(m_sel['mdd']), 0)
        m_sel['blocked'] = sim_sel['blocked']; m_sel['timeouts'] = sim_sel['timeouts']
        pd.DataFrame(sim_sel['trades']).to_csv(
            os.path.join(OUT, 'oos_trades_%s_sel.csv' % sname),
            index=False, encoding='utf-8-sig')
        # 全品种版（对照）
        ev_all = []
        for sym, sd in data.items():
            for e in gen(sd, p):
                e['_close_map'] = sd['close_map']
                if e['d'] >= CUT:
                    ev_all.append(e)
        sim_all = simulate(ev_all, days_oos, margin=margin, cap=True,
                           max_products=cap, max_hold=mh, add_max_lots=am)
        m_all = summarize(sim_all['trades'], '%s_all' % sname, mh)
        m_all['peak_margin'] = sim_all['peak_margin']
        m_all['max_conc'] = sim_all['max_concurrent']
        m_all['suggest_capital'] = round(sim_all['peak_margin'] + abs(m_all['mdd']), 0)
        m_all['blocked'] = sim_all['blocked']; m_all['timeouts'] = sim_all['timeouts']
        pd.DataFrame(sim_all['trades']).to_csv(
            os.path.join(OUT, 'oos_trades_%s_all.csv' % sname),
            index=False, encoding='utf-8-sig')
        oos_res[sname] = dict(p=p, selected=is_res[sname]['n_selected'],
                              sel=m_sel, all=m_all)
        print('[OOS][%s] p=%s  sel net=%.0f pf=%.3f win=%.1f%% n=%d MDD=%.0f 建议%.0f | all net=%.0f (%.0fs)'
              % (sname, p, m_sel['net'], m_sel['pf'], m_sel['win'], m_sel['n'],
                 m_sel['mdd'], m_sel['suggest_capital'], m_all['net'],
                 time.time() - t0), flush=True)
    return oos_res


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
                sd = V.load_symbol(sym, engine)
            except Exception as ex:
                print('LOAD-ERR %s: %s' % (sym, str(ex)[:120]), flush=True)
                sd = None
            if sd is not None:
                data[sym] = sd
        if not SMOKE:
            with open(CACHE, 'wb') as f:
                pickle.dump(data, f)
        print('cache written %d syms (%.0fs)' % (len(data), time.time() - t0), flush=True)

    if SMOKE:
        data = {k: v for k, v in data.items() if k in set(universe)}

    margin, msrc = V.load_margin_map(engine, universe)
    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    print('days=%d (%s -> %s) OOS_days=%d' % (
        len(days_global), days_global[0], days_global[-1],
        int((days_global >= CUT).sum())), flush=True)

    is_res = None
    if not OOS_ONLY:
        is_res = run_is(data, days_global, margin)
        with open('/tmp/four_is.pkl', 'wb') as f:
            pickle.dump(is_res, f)
    if not IS_ONLY:
        if is_res is None:
            with open('/tmp/four_is.pkl', 'rb') as f:
                is_res = pickle.load(f)
        oos_res = run_oos(data, days_global, margin, is_res)
        with open('/tmp/four_oos.pkl', 'wb') as f:
            pickle.dump(oos_res, f)
        # 汇总 json
        summ = {}
        for sname, r in oos_res.items():
            summ[sname] = dict(p=r['p'], selected=r['selected'],
                               sel={k: r['sel'][k] for k in
                                    ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                     'suggest_capital', 'peak_margin', 'max_conc',
                                     'n_syms', 'by_year', 'top5', 'worst5',
                                     'blocked', 'timeouts', 'per_trade')},
                               all={k: r['all'][k] for k in
                                    ('n', 'net', 'pf', 'win', 'cost', 'mdd',
                                     'suggest_capital', 'peak_margin', 'max_conc',
                                     'n_syms', 'by_year', 'blocked', 'timeouts')})
        with open(os.path.join(OUT, 'four_results.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(cut=str(CUT), n_syms=len(data),
                           oos_days=int((days_global >= CUT).sum()),
                           is_res={s: dict(best_p=v['best_p'], best_total=v['best_total'],
                                           n_selected=v['n_selected'],
                                           combos=v['combos']) for s, v in is_res.items()},
                           oos=summ), f, ensure_ascii=False, indent=1, default=str)
        print('DONE %.0fs' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
