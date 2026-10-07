# -*- coding: utf-8 -*-
"""滚动品种池筛选 walk-forward 复测（OOS 窗口 2021-01-01 → 数据末尾）。

结构（严格无未来函数）：
  Pass0 参考跑：全品种、无池限制的组合回测（2015→2026），产出「每品种按平仓日实现的盈亏序列」。
        选池只用严格早于调仓日的数据（窗口 [rb-W, rb) ）。另备 SOLO 驱动（每品种独立跑，
        不受名额竞争），用于检验结论是否依赖驱动口径。
  Pass1 在同一 OOS 事件集 / 同一日网格上重跑组合，唯一差别：ENTRY 只接受当期在池内的品种。
        在池内的老仓位照常管理、加仓、离场（不做强制轮动）。

对照：BASE（不筛）+ 若干筛选变体（调仓频率 × 回看窗口 × 入选规则）。
"""
import os
import sys
import time
import json

sys.path.insert(0, '/app')
sys.path.insert(0, '/tmp')

import numpy as np
import pandas as pd

import run_param_scan_v3 as V

OUT = '/tmp/roll_out'
os.makedirs(OUT, exist_ok=True)
SMOKE = '--smoke' in sys.argv
CUT = np.datetime64('2021-01-01')
PBASE = V.make_P(stop_scale=2.0)          # 4×ATR60 基线
MP, HOLD = 10, 30


def log(*a):
    print('[roll]', *a, flush=True)


# ------------------------- 选池驱动 -------------------------
def make_driver(df):
    """df: trades 表（含 symbol / exit_date / pnl）→ {sym: (日期数组, 累计盈亏数组)}"""
    drv = {}
    for sym, g in df.groupby('symbol'):
        d = np.array([np.datetime64(str(x)) for x in g.exit_date])
        p = g.pnl.to_numpy(float)
        o = np.argsort(d, kind='stable')
        drv[sym] = (d[o], np.cumsum(p[o]))
    return drv


def window_net(drv, sym, lo, hi):
    t = drv.get(sym)
    if t is None:
        return 0.0, 0
    d, cp = t
    i0 = int(np.searchsorted(d, lo, 'left'))
    i1 = int(np.searchsorted(d, hi, 'left'))
    if i1 <= i0:
        return 0.0, 0
    tot = cp[i1 - 1] - (cp[i0 - 1] if i0 > 0 else 0.0)
    return float(tot), i1 - i0


def build_pools(drv, rbs, win_days, rule, k=None, min_tr=5, universe=None):
    pools, info = [], []
    for rb in rbs:
        lo = rb - np.timedelta64(win_days, 'D')
        rows = []
        for sym in universe:
            net, cnt = window_net(drv, sym, lo, rb)
            if cnt < min_tr:
                continue
            rows.append((net, sym, cnt))
        rows.sort(key=lambda x: (-x[0], x[1]))
        if rule == 'POS':
            sel = {s for net, s, c in rows if net > 0}
        elif rule == 'NEG':
            sel = {s for net, s, c in rows if net <= 0}
        else:
            sel = {s for net, s, c in rows[:k]}
        pools.append(sel)
        info.append(dict(rb=str(rb), n_elig=len(rows), n_sel=len(sel),
                         sel=sorted(sel)))
    return pools, info


class PoolSel:
    def __init__(self, rbs, pools):
        self.rbs = np.array(rbs, dtype='datetime64[D]')
        self.pools = pools

    def __call__(self, d):
        j = int(np.searchsorted(self.rbs, np.datetime64(d), 'right')) - 1
        return self.pools[j] if j >= 0 else set()


def annual_rbs(start, end):
    y0 = int(str(start)[:4])
    y1 = int(str(end)[:4])
    out = []
    for y in range(y0, y1 + 1):
        d = np.datetime64('%d-01-01' % y)
        if start <= d <= end:
            out.append(d)
    return out


def semiannual_rbs(start, end):
    y0 = int(str(start)[:4])
    y1 = int(str(end)[:4])
    out = []
    for y in range(y0, y1 + 1):
        for md in ('01-01', '07-01'):
            d = np.datetime64('%d-%s' % (y, md))
            if start <= d <= end:
                out.append(d)
    return out


# ------------------------- 带池过滤的组合模拟 -------------------------
def simulate_pool(all_events, days_global, Prun, margin=None, cap=True,
                  max_products=10, max_hold=30, pool_at=None):
    """与 run_param_scan_v3.simulate 逐行一致，仅新增 pool_at 入场过滤。"""
    ev_sorted = sorted(all_events,
                       key=lambda e: (e['dt'], e['rank'], -e.get('score', 0.0)))
    day_idx = {d: i for i, d in enumerate(days_global)}
    n_days = len(days_global)
    positions = {}
    trades = []
    blocked = timeouts = filtered = 0
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

    def close_pos(sym, dq, reason, price):
        nonlocal total_lots, cur_margin, cur_notional
        pos = positions.pop(sym)
        total_lots -= len(pos['lots'])
        ii = day_idx[dq]
        raw_e, sig_e = price
        for li, lot in enumerate(pos['lots']):
            c_open, mult = V.CB.half(sym, lot['d'], lot['px_raw'])
            c_close, _ = V.CB.half(sym, ii2date(ii), raw_e)
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
                    close_pos(sym, dchk, 'TIMEOUT', pr)
                    timeouts += 1
        i_checked = di
        while k < ne and ev_sorted[k]['dt'] == dt0:
            e = ev_sorted[k]
            k += 1
            sym = e['sym']
            if e['kind'] == 'EXIT':
                if sym in positions:
                    close_pos(sym, e['d'], 'SIGNAL', (e['px_raw'], e['px_adj']))
            elif e['kind'] == 'ADD':
                pos = positions.get(sym)
                if pos is None or len(pos['lots']) >= Prun.add_max_lots + 1:
                    continue
                if cap and total_lots >= total_lots_cap:
                    continue
                m = lot_margin(sym, e['px_raw'], pos['dir'])
                mult = V.CB.coef(sym, e['d'])['multiplier']
                pos['lots'].append(dict(d=e['d'], px_raw=e['px_raw'],
                                        px_adj=e['px_adj'],
                                        entry_i=day_idx[e['d']], margin=m))
                total_lots += 1
                cur_margin += m
                cur_notional += e['px_raw'] * mult
                if cur_margin > peak_margin:
                    peak_margin, peak_margin_day = cur_margin, str(e['d'])
                peak_notional = max(peak_notional, cur_notional)
            elif e['kind'] == 'ENTRY':
                if sym in positions:
                    continue
                if pool_at is not None and sym not in pool_at(e['d']):
                    filtered += 1
                    continue
                if cap and (len(positions) >= max_products
                            or total_lots >= total_lots_cap):
                    blocked += 1
                    continue
                m = lot_margin(sym, e['px_raw'], e['dir'])
                mult = V.CB.coef(sym, e['d'])['multiplier']
                positions[sym] = dict(
                    dir=e['dir'], score=e.get('score', 0.0),
                    close_map=e['_close_map'],
                    lots=[dict(d=e['d'], px_raw=e['px_raw'], px_adj=e['px_adj'],
                               entry_i=day_idx[e['d']], margin=m)])
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
                close_pos(sym, dchk, 'TIMEOUT', pr)
                timeouts += 1
    last_d = days_global[-1]
    for sym in list(positions):
        pos = positions[sym]
        pr = day_pos(last_d, pos['close_map'])
        if pr is None:
            pr = (pos['lots'][-1]['px_raw'], pos['lots'][-1]['px_adj'])
        close_pos(sym, last_d, 'FORCE_END', pr)
    return dict(trades=trades, blocked=blocked, timeouts=timeouts,
                filtered=filtered, max_concurrent=max_conc,
                peak_margin=round(peak_margin, 0),
                peak_notional=round(peak_notional, 0),
                peak_margin_day=peak_margin_day)


def equity_curve(df, days):
    """按平仓日累计（已实现）→ 净值序列 + 最大回撤。"""
    if not len(df):
        return 0.0, []
    s = df.groupby('exit_date').pnl.sum().sort_index()
    cum = s.cumsum()
    mdd = float((cum - cum.cummax()).min())
    return mdd, [(str(k), float(v)) for k, v in cum.items()]


# ------------------------- 主流程 -------------------------
def main():
    V._isolate_cost_connections()
    engine = V.get_engine()
    t0 = time.time()
    universe, skipped = V.build_universe(engine)
    if SMOKE:
        universe = universe[:8]
    log('universe=%d skipped=%d' % (len(universe), len(skipped)))

    data = V.pickle.load(open(V.CACHE, 'rb'))
    if set(data.keys()) != set(universe):
        data = {k: v for k, v in data.items() if k in set(universe)}
    log('data loaded %d syms (%.0fs)' % (len(data), time.time() - t0))
    universe = sorted(data.keys())

    margin, msrc = V.load_margin_map(engine, universe)
    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    close_map = {sd['sym']: sd['close_map'] for sd in data.values()}
    log('days: %d  (%s → %s)' % (len(days_global), days_global[0], days_global[-1]))

    # ---- 事件只生成一次 ----
    t1 = time.time()
    ev_all = []
    for sym, sd in data.items():
        ev = V.gen_events(sd, PBASE, 2.0, False)
        for e in ev:
            e['_close_map'] = close_map[sym]
        ev_all.extend(ev)
    log('events=%d (%.0fs)' % (len(ev_all), time.time() - t1))

    # ---- Pass0a：参考跑（全品种、无池限制、全历史）→ 驱动 A ----
    t1 = time.time()
    ref = V.simulate(ev_all, days_global, PBASE, margin=margin,
                     max_products=MP, max_hold=HOLD)
    df_ref = pd.DataFrame(ref['trades'])
    log('REF  n=%d net=%.0f (%.0fs)' % (len(df_ref), df_ref.pnl.sum(),
                                        time.time() - t1))
    drvA = make_driver(df_ref)

    # ---- Pass0b：SOLO 驱动（每品种独立跑，不受名额竞争）----
    t1 = time.time()
    solo_rows = []
    for sym in universe:
        ev_s = [e for e in ev_all if e['sym'] == sym]
        s1 = V.simulate(ev_s, days_global, PBASE, margin=margin,
                        cap=False, max_products=9999, max_hold=HOLD)
        for tr in s1['trades']:
            solo_rows.append(tr)
    df_solo = pd.DataFrame(solo_rows)
    drvB = make_driver(df_solo)
    log('SOLO n=%d net=%.0f (%.0fs)' % (len(df_solo), df_solo.pnl.sum(),
                                        time.time() - t1))

    # ---- OOS 事件集 / 日网格 ----
    days_oos = days_global[days_global >= CUT]
    ev_oos = [e for e in ev_all if e['d'] >= CUT]
    log('OOS days=%d events=%d (%s → %s)'
        % (len(days_oos), len(ev_oos), days_oos[0], days_oos[-1]))

    rb_a = annual_rbs(CUT, days_oos[-1])
    rb_s = semiannual_rbs(CUT, days_oos[-1])
    log('rebal annual=%s semiannual=%d' % ([str(x) for x in rb_a], len(rb_s)))

    def mk(drv, rbs, win, rule, k=None, min_tr=5):
        pools, info = build_pools(drv, rbs, win, rule, k=k, min_tr=min_tr,
                                  universe=universe)
        return PoolSel(rbs, pools), info

    CFG = []
    CFG.append(('BASE_nofilter', None))
    CFG.append(('A_1Y_W3_POS', mk(drvA, rb_a, 1095, 'POS')))
    CFG.append(('A_1Y_W3_TOP10', mk(drvA, rb_a, 1095, 'TOP', k=10)))
    CFG.append(('A_1Y_W3_TOP15', mk(drvA, rb_a, 1095, 'TOP', k=15)))
    CFG.append(('A_1Y_W5_POS', mk(drvA, rb_a, 1825, 'POS')))
    CFG.append(('A_6M_W3_POS', mk(drvA, rb_s, 1095, 'POS')))
    CFG.append(('A_6M_W3_TOP15', mk(drvA, rb_s, 1095, 'TOP', k=15)))
    CFG.append(('B_1Y_W3_POS', mk(drvB, rb_a, 1095, 'POS')))
    CFG.append(('A_1Y_W3_NEG', mk(drvA, rb_a, 1095, 'NEG')))

    results = {}
    for name, cfg in CFG:
        t1 = time.time()
        sel, info = (None, None) if cfg is None else cfg
        sim = simulate_pool(ev_oos, days_oos, PBASE, margin=margin,
                            max_products=MP, max_hold=HOLD, pool_at=sel)
        df = pd.DataFrame(sim['trades'])
        if not len(df):
            log('%-16s 无交易' % name)
            continue
        net = float(df.pnl.sum())
        wins, losses = df[df.pnl > 0], df[df.pnl <= 0]
        pf = (float(wins.pnl.sum() / abs(losses.pnl.sum()))
              if len(losses) and losses.pnl.sum() != 0 else float('inf'))
        mdd, eq = equity_curve(df, days_oos)
        by_year = {str(y): round(float(g.pnl.sum()), 0)
                   for y, g in df.groupby('eyear')}
        pool_sz = [x['n_sel'] for x in info] if info else []
        m = dict(name=name, n=int(len(df)), net=round(net, 0),
                 pf=round(pf, 3), win=round(100 * len(wins) / len(df), 1),
                 cost=round(float(df.cost.sum()), 0),
                 per_trade=round(net / len(df), 0),
                 n_syms=int(df.symbol.nunique()),
                 peak_margin=sim['peak_margin'], mdd=round(mdd, 0),
                 suggest_capital=round(sim['peak_margin'] + abs(mdd), 0),
                 blocked=sim['blocked'], filtered=sim['filtered'],
                 timeouts=sim['timeouts'], max_conc=sim['max_concurrent'],
                 by_year=by_year,
                 pool_size_avg=(round(float(np.mean(pool_sz)), 1) if pool_sz else None),
                 pool_size_min=(int(min(pool_sz)) if pool_sz else None),
                 pool_size_max=(int(max(pool_sz)) if pool_sz else None),
                 top5=[dict(s=s, v=round(float(v), 0)) for s, v in
                       df.groupby('symbol').pnl.sum().sort_values(ascending=False).head(5).items()])
        results[name] = m
        df.to_csv(os.path.join(OUT, 'trades_%s.csv' % name),
                  index=False, encoding='utf-8-sig')
        log('%-16s n=%-6d net=%-11.0f pf=%.3f win=%4.1f%% 池=%s 过滤%d 峰值保证金%.0f MDD%.0f 建议%.0f (%.0fs)'
            % (name, m['n'], m['net'], m['pf'], m['win'],
               ('%d-%d(均%.1f)' % (m['pool_size_min'], m['pool_size_max'],
                                   m['pool_size_avg'])) if pool_sz else '-',
               m['filtered'], m['peak_margin'], m['mdd'], m['suggest_capital'],
               time.time() - t1))

    with open(os.path.join(OUT, 'results.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(cut=str(CUT), n_syms=len(universe),
                       oos_days=len(days_oos), oos_events=len(ev_oos),
                       ref_net=round(float(df_ref.pnl.sum()), 0),
                       ref_n=int(len(df_ref)),
                       solo_net=round(float(df_solo.pnl.sum()), 0),
                       rebal_annual=[str(x) for x in rb_a],
                       rebal_semiannual=[str(x) for x in rb_s],
                       pools={n: (c[1] if c else None) for n, c in CFG},
                       results=results), f,
                  ensure_ascii=False, indent=1, default=str)
    log('DONE %.0fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
