"""第三轮：融合策略多参数扫描（60m 信号层）。

与 run_stop_cali_v2 的差别：
  * 每个 run 可覆盖任意 P 字段（trail_atr / be_r / adx_min / cooldown_bars /
    add_max_lots / stop_scale / use_daily），不再只扫 stop_scale；
  * gen_events / simulate 改为接收运行时 P，不再读全局；
  * 事件不再按 stop_scale 缓存（参数组合各不相同）。
其余口径（信号空间、成本、保证金、品种池、IS/OOS 切分）与 v2 完全一致。
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
from sqlalchemy import text as T

from app.core.db import get_engine
from app.data.back_adjust import apply_back_adjust_from_materialized
from app.data.cost import cost_coefficients, cost_yuan_at, CostNotFoundError
# 2026-10-07 CB 收纳时修正：原先 import 扁平模块名 `fusion_signal_exp`
# （该模块只存在于 WB 手工 docker cp 的 /tmp 桥接文件，镜像重建即断链——
#  工单 A1）。现改为与相邻行一致的规范导入；别名模块
#  app/strategies/fusion_signal_exp.py 仍保留，供其它未知调用方使用。
from app.strategies.fusion_signal import walk_fusion_states, ema, atr14, adx14

OUT = '/tmp/scan3_out'
CACHE = '/tmp/stop_cali_cache.pkl'      # 与 v1/v2 共用（load_symbol 结构未变）
os.makedirs(OUT, exist_ok=True)

WARMUP_DAYS = 200
IS_LAST_YEAR = 2020

BASE = dict(ema_k=140, atr_n=14, ma_n=20, sl_atr=2.0, trail_atr=2.0, be_r=0.5,
            W=60, entry_mode='both_nm', cooldown_bars=3,
            adx_n=14, adx_min=15.0, use_sbull=False, fib_confl=True,
            fib_ratios=(0.382, 0.5, 0.618), fib_tol_atr=0.5,
            add_max_lots=2, add_thr_atr=1.0, add_guard_atr=0.0)

NOCOST = {'IF888', 'LR888', 'T888', 'TF888', 'TL888', 'TS888'}


def make_P(**ov):
    d = dict(BASE)
    d.update(ov)
    return SimpleNamespace(**d)


P = make_P()          # 仅用于预计算指标（ma_n/atr_n/adx_n 与 BASE 一致）


def log(*a):
    print('[scan3]', *a, flush=True)


# ---------------- 连接池隔离 ----------------
def _isolate_cost_connections():
    """fee_per_lot 的 fallback 分支会泄漏池连接；把成本模块的会话切到 NullPool。"""
    from sqlalchemy import create_engine
    from app.core import db as dbmod
    url = dbmod.get_engine().url.render_as_string(hide_password=False)
    eng = create_engine(url, poolclass=__import__(
        'sqlalchemy.pool', fromlist=['NullPool']).NullPool)
    import app.data.cost as cost_mod
    import app.data.barstore as bar_mod
    for m in (cost_mod, bar_mod):
        for attr in ('_engine', 'get_engine'):
            if hasattr(m, attr):
                try:
                    setattr(m, attr, lambda: eng)
                except Exception:
                    pass
    cost_mod._engine = lambda: eng
    if hasattr(cost_mod, 'get_engine'):
        cost_mod.get_engine = lambda: eng
    bar_mod._engine = lambda: eng
    if hasattr(bar_mod, 'get_engine'):
        bar_mod.get_engine = lambda: eng


# ---------------- 品种池 ----------------
def build_universe(engine):
    with engine.connect() as c:
        b60 = pd.read_sql(T("""SELECT symbol, min(bucket) s, max(bucket) e, count(*) n
                               FROM l1_mkt.bar_60m
                               WHERE symbol LIKE '%888' AND symbol NOT LIKE '%8888'
                               GROUP BY 1"""), c)
        db = pd.read_sql(T("""SELECT symbol, min(trade_date) s
                              FROM l0_raw.daily_bar
                              WHERE symbol LIKE '%888' AND symbol NOT LIKE '%8888'
                              GROUP BY 1"""), c)
        rs = set(r[0] for r in c.execute(T(
            "SELECT DISTINCT symbol FROM l2_adj.roll_segment WHERE freq='min60'")).all())
    db = dict(zip(db.symbol, db.s))
    universe, skipped = [], {}
    for _, r in b60.iterrows():
        sym = r.symbol
        if sym in NOCOST:
            skipped[sym] = 'no_cost'
            continue
        if sym not in rs:
            skipped[sym] = 'no_roll_seg'
            continue
        if sym not in db:
            skipped[sym] = 'no_daily'
            continue
        gap = (pd.Timestamp(db[sym]).tz_localize(None)
               - pd.Timestamp(r.s).tz_localize(None)).days
        if gap > 400:
            skipped[sym] = 'daily_gap_%dd' % gap
            continue
        try:
            cost_coefficients(sym, on_date=pd.Timestamp(r.s).date())
        except CostNotFoundError:
            skipped[sym] = 'no_cost'
            continue
        universe.append(sym)
    return sorted(universe), skipped


# ---------------- 保证金 ----------------
def load_margin_map(engine, universe):
    from app.data.barstore import variety_of, variety_spec
    with engine.connect() as c:
        cm = {r[0]: r[1] for r in c.execute(T(
            "SELECT variety_code, broker_margin_ratio FROM l3_ref.cfg_symbol_margin")).all()
            if r[1] is not None}
        dv = {}
        for r in c.execute(T("""SELECT variety_code, multiplier, margin_rate_long,
                                       margin_rate_short
                                FROM l3_ref.dim_variety
                                WHERE multiplier IS NOT NULL""")).all():
            dv[r[0]] = (float(r[1]), r[2], r[3])
    mm, src = {}, {}
    for sym in universe:
        code = variety_of(sym)
        mult = rate_l = rate_s = None
        if code in dv:
            mult, rl, rs_ = dv[code]
            rate_l = float(rl) if rl is not None else None
            rate_s = float(rs_) if rs_ is not None else None
        if mult is None:
            try:
                mult = float(variety_spec(sym)['multiplier'])
            except Exception:
                mult = None
        if code in cm:
            rate_l = rate_s = float(cm[code])
            src[sym] = 'broker'
        elif rate_l is not None or rate_s is not None:
            rate_l = max(x for x in (rate_l, rate_s) if x is not None)
            rate_s = rate_l
            src[sym] = 'exchange_max'
        else:
            rate_l = rate_s = 0.12
            src[sym] = 'default_12pct'
        mult = mult or 10.0
        mm[sym] = (mult * rate_l, mult * rate_s)
    return mm, src


# ---------------- 每品种静态数据 ----------------
def load_symbol(sym, engine):
    with engine.connect() as c:
        b60 = pd.read_sql(T("""SELECT bucket, open, high, low, close
                               FROM l1_mkt.bar_60m WHERE symbol=:s ORDER BY bucket"""),
                          c, params={'s': sym})
        db = pd.read_sql(T("""SELECT trade_date, open, high, low, close
                              FROM l0_raw.daily_bar WHERE symbol=:s ORDER BY trade_date"""),
                         c, params={'s': sym})
    if len(b60) < 200 or len(db) < 200:
        return None
    dt = pd.to_datetime(b60['bucket'])
    if dt.dt.tz is None:
        dt = dt.dt.tz_localize('Asia/Shanghai')
    else:
        dt = dt.dt.tz_convert('Asia/Shanghai')
    b60['dt'] = dt
    b60['d'] = dt.dt.date

    with engine.connect() as _c:
        sig = apply_back_adjust_from_materialized(b60, sym, 'min60', _c)
    o = sig['open'].to_numpy(float)
    h = sig['high'].to_numpy(float)
    l = sig['low'].to_numpy(float)
    cl = sig['close'].to_numpy(float)
    raw_close = b60['close'].to_numpy(float)

    n = len(b60)
    dclose = db['close'].to_numpy(float)
    dema = ema(dclose, 140)
    ddir = np.where(dclose > dema, 1, -1)
    datr = atr14(db['high'].to_numpy(float), db['low'].to_numpy(float), dclose, 14)
    dbd = np.array([pd.Timestamp(x).date() for x in db['trade_date']],
                   dtype='datetime64[D]')
    bd = np.array([np.datetime64(x) for x in b60['d']], dtype='datetime64[D]')
    pos = np.searchsorted(dbd, bd, side='left') - 1
    valid = pos >= 0
    htf_dir = np.zeros(n, dtype=int)
    htf_dir[valid] = ddir[pos[valid]]
    daily_atr = np.full(n, np.nan)
    daily_atr[valid] = datr[pos[valid]]

    g = b60.groupby('d', sort=True).agg(raw_last=('close', 'last'))
    day_dates = np.array([np.datetime64(x) for x in g.index], dtype='datetime64[D]')
    close_map = (day_dates, g['raw_last'].to_numpy(float),
                 pd.Series(cl, index=b60.index).groupby(b60['d']).last().to_numpy(float))

    ma20 = ema(cl, P.ma_n)
    adx_arr = adx14(h, l, cl, P.adx_n)
    atr_arr = atr14(h, l, cl, P.atr_n)

    sym_cut = (pd.Timestamp(b60['d'].iloc[0]) + pd.Timedelta(days=WARMUP_DAYS)).date()
    return dict(sym=sym, o=o, h=h, l=l, c=cl, raw_close=raw_close,
                dts=b60['dt'].tolist(), days=[np.datetime64(x) for x in b60['d']],
                htf_dir=htf_dir, daily_atr=daily_atr, close_map=close_map,
                ma20=ma20, adx=adx_arr, atr=atr_arr, sym_cut=sym_cut)


# ---------------- 事件生成 ----------------
def gen_events(sd, Prun, stop_scale, use_daily_atr):
    sym = sd['sym']
    kw = dict(stop_scale=stop_scale)
    if use_daily_atr:
        kw['daily_atr'] = sd['daily_atr']
    cutn = np.datetime64(sd['sym_cut'])
    events = []
    prev_state, prev_lots, prev_entry_i = 0, 0, -1
    k = 0
    for fr in walk_fusion_states(sd['o'], sd['h'], sd['l'], sd['c'],
                                 sd['htf_dir'], Prun, **kw):
        i = k + 2
        k += 1
        d = sd['days'][i]
        if d < cutn:
            continue
        st, lots, ei = fr['state'], fr['lots'], fr['entry_i']
        dt = sd['dts'][i]
        px_raw = float(sd['raw_close'][i])
        px_adj = float(sd['c'][i])
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
            adx_v = float(sd['adx'][i])
            atr_v = float(sd['atr'][i])
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
    return events


# ---------------- 成本 ----------------
class CostBook:
    def __init__(self):
        self.cache = {}

    def coef(self, sym, d):
        key = (sym, pd.Timestamp(d).year)
        c = self.cache.get(key)
        if c is None:
            c = cost_coefficients(sym, on_date=pd.Timestamp(d).date())
            self.cache[key] = c
        return c

    def half(self, sym, d, px_raw):
        c = self.coef(sym, d)
        return 0.5 * cost_yuan_at(c, px_raw), c['multiplier']


CB = CostBook()


# ---------------- 组合模拟 ----------------
def simulate(all_events, days_global, Prun, margin=None, cap=True,
             max_products=10, max_hold=30):
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

    def close_pos(sym, dq, reason, price):
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
                mult = CB.coef(sym, e['d'])['multiplier']
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
                max_concurrent=max_conc, peak_margin=round(peak_margin, 0),
                peak_notional=round(peak_notional, 0), peak_margin_day=peak_margin_day)


# ---------------- 指标汇总 ----------------
def summarize(trades, label):
    if not trades:
        return dict(label=label, n=0, net=0.0)
    df = pd.DataFrame(trades)
    net = float(df.pnl.sum())
    wins = df[df.pnl > 0]
    losses = df[df.pnl <= 0]
    pf = (float(wins.pnl.sum() / abs(losses.pnl.sum()))
          if len(losses) and losses.pnl.sum() != 0 else float('inf'))
    by_year = {str(y): dict(n=int(len(g)), net=round(float(g.pnl.sum()), 0))
               for y, g in df.groupby('eyear')}
    buckets = {}
    for lo, hi in ((0, 3), (3, 10), (10, 30), (30, 100000)):
        g = df[(df.hold_days >= lo) & (df.hold_days < hi)]
        buckets['%d-%s' % (lo, 'inf' if hi > 9999 else hi)] = dict(
            n=int(len(g)), net=round(float(g.pnl.sum()), 0),
            win=round(100 * len(g[g.pnl > 0]) / len(g), 1) if len(g) else 0.0)
    by_sym = df.groupby('symbol').pnl.sum().sort_values()
    is_df = df[df.eyear <= IS_LAST_YEAR]
    oos_df = df[df.eyear > IS_LAST_YEAR]
    return dict(label=label, n=int(len(df)), net=round(net, 0),
                win=round(100 * len(wins) / len(df), 1), pf=round(pf, 3),
                cost=round(float(df.cost.sum()), 0),
                net_is=round(float(is_df.pnl.sum()), 0), n_is=int(len(is_df)),
                net_oos=round(float(oos_df.pnl.sum()), 0), n_oos=int(len(oos_df)),
                pos_years=int(sum(1 for v in by_year.values() if v['net'] > 0)),
                n_years=len(by_year), by_year=by_year, buckets=buckets,
                top5=[dict(s=s, v=round(float(v), 0)) for s, v in
                      df.groupby('symbol').pnl.sum().sort_values(ascending=False).head(5).items()],
                worst5=[dict(s=s, v=round(float(v), 0)) for s, v in by_sym.head(5).items()],
                n_syms=int(df.symbol.nunique()))


# ---------------- 扫描配置 ----------------
def build_runs():
    """基线 = B 口径（stop_scale=2.0 → 初始止损 4×60m ATR），其余为 BASE 默认。"""
    R = []

    def add(name, ov, mp=10, hold=30):
        R.append((name, ov, mp, hold))

    # 组1：跟踪止损（初始止损已放宽到 4×ATR，trail 是否该同步放宽是核心问题）
    add('TR00_trail_off', dict(stop_scale=2.0, trail_atr=0.0))
    add('TR15', dict(stop_scale=2.0, trail_atr=1.5))
    add('TR20_base', dict(stop_scale=2.0, trail_atr=2.0))
    add('TR30', dict(stop_scale=2.0, trail_atr=3.0))
    add('TR40', dict(stop_scale=2.0, trail_atr=4.0))
    add('TR50', dict(stop_scale=2.0, trail_atr=5.0))
    # 组2：保本触发 R 倍数
    add('BE00_off', dict(stop_scale=2.0, be_r=0.0))
    add('BE10', dict(stop_scale=2.0, be_r=1.0))
    add('BE20', dict(stop_scale=2.0, be_r=2.0))
    # 组3：ADX 门控
    add('AX10', dict(stop_scale=2.0, adx_min=10.0))
    add('AX20', dict(stop_scale=2.0, adx_min=20.0))
    add('AX25', dict(stop_scale=2.0, adx_min=25.0))
    # 组4：冷却 bar 数
    add('CD0', dict(stop_scale=2.0, cooldown_bars=0))
    add('CD6', dict(stop_scale=2.0, cooldown_bars=6))
    add('CD12', dict(stop_scale=2.0, cooldown_bars=12))
    # 组5：加仓手数上限
    add('AD0_noadd', dict(stop_scale=2.0, add_max_lots=0))
    add('AD1', dict(stop_scale=2.0, add_max_lots=1))
    add('AD3', dict(stop_scale=2.0, add_max_lots=3))
    # 组6：更宽初始止损（配合 15m 等距离对照：15m 的 8/12/16×ATR15 ≈ 60m 的 4/6/8×ATR60）
    add('G_8x60m', dict(stop_scale=4.0))
    add('H_10x60m', dict(stop_scale=5.0))
    return R


def main():
    _isolate_cost_connections()
    engine = get_engine()
    t0 = time.time()
    smoke = '--smoke' in sys.argv
    universe, skipped = build_universe(engine)
    if smoke:
        universe = universe[:3]
    log('universe=%d skipped=%d' % (len(universe), len(skipped)))
    for k, v in sorted(skipped.items()):
        log('  skip %s: %s' % (k, v))

    data = None
    if os.path.exists(CACHE) and not smoke:
        try:
            with open(CACHE, 'rb') as f:
                data = pickle.load(f)
            if set(data.keys()) != set(universe):
                log('cache stale -> rebuild')
                data = None
            else:
                log('cache loaded %d syms (%.0fs)' % (len(data), time.time() - t0))
        except Exception as ex:
            log('cache err %s' % str(ex)[:100])
            data = None
    if data is None:
        data = {}
        for i, sym in enumerate(universe):
            try:
                sd = load_symbol(sym, engine)
            except Exception as ex:
                log('LOAD-ERR %s: %s' % (sym, str(ex)[:120]))
                sd = None
            if sd is not None:
                data[sym] = sd
            if (i + 1) % 10 == 0:
                log('loaded %d/%d (%.0fs)' % (i + 1, len(universe), time.time() - t0))
        if not smoke:
            with open(CACHE, 'wb') as f:
                pickle.dump(data, f)
        log('cache written %d syms (%.0fs)' % (len(data), time.time() - t0))

    margin, msrc = load_margin_map(engine, universe)
    log('margin: %d syms, broker=%d' % (len(margin), sum(1 for v in msrc.values() if v == 'broker')))

    all_days = sorted({d for sd in data.values() for d in sd['days']})
    days_global = np.array(all_days, dtype='datetime64[D]')
    log('trading days=%d' % len(days_global))
    close_map_by_sym = {sd['sym']: sd['close_map'] for sd in data.values()}

    runs = build_runs()
    sel = [a for a in sys.argv[1:] if not a.startswith('-')]
    if sel:
        runs = [r for r in runs if r[0] in sel]
    if smoke:
        runs = runs[:2]
    log('runs=%s' % [r[0] for r in runs])
    results = {}
    for ri, (name, ov, mp, hold) in enumerate(runs):
        t1 = time.time()
        Prun = make_P(**ov)
        stop_scale = ov.get('stop_scale', 1.0)
        use_daily = ov.get('use_daily', False)
        ev_all = []
        for sym, sd in data.items():
            ev = gen_events(sd, Prun, stop_scale, use_daily)
            for e in ev:
                e['_close_map'] = close_map_by_sym[sym]
            ev_all.extend(ev)
        sim = simulate(ev_all, days_global, Prun, margin=margin,
                       max_products=mp, max_hold=hold)
        m = summarize(sim['trades'], name)
        m['meta'] = dict(blocked=sim['blocked'], timeouts=sim['timeouts'],
                         max_concurrent=sim['max_concurrent'],
                         peak_margin=sim['peak_margin'],
                         peak_notional=sim['peak_notional'],
                         max_products=mp, max_hold=hold)
        m['ov'] = ov
        results[name] = m
        df = pd.DataFrame(sim['trades'])
        if len(df):
            df.to_csv(os.path.join(OUT, 'trades_%s.csv' % name),
                      index=False, encoding='utf-8-sig')
        log('[%d/%d] %-16s n=%-6d net=%-12.0f pf=%.3f win=%.1f%% cost=%.0f (%.0fs)'
            % (ri + 1, len(runs), name, m['n'], m['net'], m['pf'], m['win'],
               m['cost'], time.time() - t1))
        with open(os.path.join(OUT, 'results.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(universe=universe, skipped=skipped,
                           n_syms=len(data), trading_days=len(days_global),
                           base_params=BASE, results=results), f,
                      ensure_ascii=False, indent=1, default=str)
    log('DONE %.0fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
