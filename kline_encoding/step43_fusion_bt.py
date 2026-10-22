# -*- coding: utf-8 -*-
"""step43 融合策略 V3.4 忠实复刻回测 + 研究结论融合变体（A1/A2/B1/B2/B3/COMBO）

策略规格：逐行移植 app/strategies/fusion_signal.py::walk_fusion_states（V3.4 定稿 2026-09-21）
  方向层：小时线 EMA140，前一根方向门控（ok_l = dir>=0 / ok_s = dir<=0）
  入场：MA20 回踩重启（use_sbull=False 无收强要求）+ 10 根突破（both_nm 去 MACD）
  门控：ADX14 ≥ 15（上一根判定）+ 斐波汇流（仅回踩支路，W=60 枢轴窗口，0.382/0.5/0.618 ± 0.5ATR）
  离场：2×ATR 初始止损 + 2×ATR 吊灯（收盘极值）+ 0.5×ATR 保本（粘性）；close-only 触发
  加码：P1 利弗莫尔阶梯，add_max_lots=2（总手数上限），浮盈 ≥ lots×1.0×ATR 且收盘创入场以来极值
  冷却：3 根；入场门槛 i>=30；成交价 = 信号根收盘
  成本：单边佣金 1.5bp + 单边滑点 1 tick

增强变体（trading_takeaways 可执行清单 → V3.4 映射）：
  A1 高波日换档：昨日日振幅 > 滚动250日80分位 → 当日新开仓手数×0.5 且 sl/trail 距离×1.34
  A2 板块簇上限：同簇（聚酯/聚烯烃/两碱/醇醚）同时持仓 ≤2，按入场时间全局贪心过滤
  B1 极端收盘延迟：信号根 clv(0-1) 顺势极值（多≥0.8/空≤0.2）→ 次根 0.25ATR 限价，未触则次根收盘
  B2 开盘根不开仓：每 td 夜盘首根 + 日盘首根跳过
  B3 尾盘根不开仓：每 td 夜盘末根 + 日盘末根跳过
输出：results_fusion_bt.json + trades_BASE.csv / trades_COMBO.csv
"""
import json
import os
import numpy as np
import pandas as pd
from ke_common import load

PARTS_ALL = ['2020', '2021_2025', '2026']


def parts_for(sym):
    return [p for p in PARTS_ALL
            if os.path.exists(f'{sym}_{p}_1m_raw.csv') and os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
SYMS = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'FG', 'SA', 'PF', 'EB', 'UR', 'PX', 'PR', 'SC', 'IF']
MULT = {'MA': 10, 'EG': 10, 'TA': 5, 'PP': 5, 'L': 5, 'V': 5, 'UR': 20, 'FG': 20, 'SA': 20,
        'PF': 5, 'PX': 5, 'PR': 15, 'EB': 5, 'SC': 1000, 'IF': 300}   # PR=瓶片 15吨/手（假设值）
TICK = {'MA': 1, 'EG': 1, 'TA': 2, 'PP': 1, 'L': 1, 'V': 1, 'UR': 1, 'FG': 1, 'SA': 1,
        'PF': 2, 'PX': 2, 'PR': 2, 'EB': 1, 'SC': 0.1, 'IF': 0.2}
CLUSTER = {'TA': '聚酯', 'PX': '聚酯', 'PF': '聚酯', 'PR': '聚酯',
           'PP': '聚烯烃', 'L': '聚烯烃', 'V': '聚烯烃',
           'FG': '两碱', 'SA': '两碱', 'MA': '醇醚', 'EG': '醇醚', 'UR': '醇醚'}
COMM_BP = 1.5

# V3.4 默认参数（app/core/config.py 实测值）
MA_N, ATR_N, EMA_K = 20, 14, 140
SL_ATR, TRAIL_ATR, BE_R = 2.0, 2.0, 0.5
W_PIVOT, COOLDOWN = 60, 3
ADX_N, ADX_MIN = 14, 15.0
FIB_RATIOS, FIB_TOL = (0.382, 0.5, 0.618), 0.5
ADD_MAX_LOTS, ADD_THR = 2, 1.0
ENTRY_MIN_I = 30


def ema(s, n):
    return pd.Series(np.asarray(s, float)).ewm(span=n, adjust=False).mean().to_numpy()


def atr14(h, l, c, n=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    a = 1.0 / n
    for i in range(n, len(tr)):
        out[i] = a * tr[i] + (1 - a) * out[i - 1]
    return out


def adx14(h, l, c, n=14):
    cprev = np.concatenate([[c[0]], c[:-1]])
    hprev = np.concatenate([[h[0]], h[:-1]])
    lprev = np.concatenate([[l[0]], l[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - cprev), np.abs(l - cprev)])
    up = h - hprev
    dn = lprev - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr_ = pd.Series(tr).ewm(span=n, adjust=False).mean().to_numpy()
    pp = pd.Series(pdm).ewm(span=n, adjust=False).mean().to_numpy()
    mm = pd.Series(mdm).ewm(span=n, adjust=False).mean().to_numpy()
    pdi = 100 * pp / (atr_ + 1e-9)
    mdi = 100 * mm / (atr_ + 1e-9)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-9)
    ad = pd.Series(dx).ewm(span=n, adjust=False).mean().to_numpy().copy()
    ad[:2 * n] = 0.0
    return ad


def build_h1(sym):
    """1m → 小时线（td × 时钟小时桶）+ 日线（A1 高波判定）。"""
    d = load(sym, parts_for(sym))
    d['hh'] = d.datetime.dt.hour.values
    g = d.groupby(['td', 'hh'], sort=True)
    h1 = g.agg(o=('open', 'first'), h=('high', 'max'), l=('low', 'min'), c=('close', 'last'),
               dt=('datetime', 'first')).reset_index()
    h1 = h1.sort_values('dt').reset_index(drop=True)
    h1['is_night'] = (h1.hh >= 18) | (h1.hh <= 8)
    first_b = np.zeros(len(h1), bool)
    last_b = np.zeros(len(h1), bool)
    td_vals = h1.td.values
    night = h1.is_night.values
    i = 0
    while i < len(h1):
        j = i
        while j < len(h1) and td_vals[j] == td_vals[i]:
            j += 1
        seg = np.arange(i, j)
        ni = seg[night[seg]]
        di = seg[~night[seg]]
        if len(ni):
            first_b[ni[0]] = True
            last_b[ni[-1]] = True
        if len(di):
            first_b[di[0]] = True
            last_b[di[-1]] = True
        i = j
    h1['sess_first'] = first_b
    h1['sess_last'] = last_b
    # 日线（td 聚合，夜盘已归次日）→ A1 用昨日振幅分位
    dd = h1.groupby('td', sort=True).agg(o=('o', 'first'), h=('h', 'max'), l=('l', 'min'),
                                         c=('c', 'last')).reset_index()
    pc_d = dd.c.shift(1)
    dd['amp'] = (dd.h - dd.l) / pc_d
    for q in [50, 60, 70, 75, 80, 85, 90, 95]:
        dd[f'amp_q{q}'] = dd.amp.rolling(250, min_periods=60).quantile(q / 100.0)
        dd[f'hvol{q}'] = (dd.amp > dd[f'amp_q{q}']).shift(1).fillna(False)   # 昨日判定→今日生效
        h1[f'hvol{q}'] = h1.td.map(dd.set_index('td')[f'hvol{q}']).fillna(False).values.astype(bool)
    dd['lret'] = np.log(dd.c / dd.c.shift(1))
    h1['hvol'] = h1['hvol80']
    h1['year'] = pd.to_datetime(h1.dt).dt.year.values
    return h1


def cluster_shock_days(data):
    """A3：簇日收益 |r| > 3σ（滚动250日）→ 该簇当日封禁新开仓（任一成员暴动则全簇停）。"""
    daily = {}
    for sym, h1 in data.items():
        cl = CLUSTER.get(sym)
        if cl is None:
            continue
        dd = h1.groupby('td', sort=True).agg(c=('c', 'last')).reset_index()
        dd['lret'] = np.log(dd.c / dd.c.shift(1))
        daily.setdefault(cl, []).append(dd.set_index('td').lret)
    out = {}
    for cl, serlist in daily.items():
        r = pd.concat(serlist, axis=1).mean(axis=1)   # 簇等权日收益
        sd = r.rolling(250, min_periods=60).std()
        shock = (r.abs() > 3 * sd).fillna(False)
        out[cl] = set(r.index[shock.values])
    return out


def run_symbol(sym, h1, variant, cfg=None):
    cfg = cfg or {}
    """逐行移植 walk_fusion_states + 成本/变体。variant: set ⊆ {'A1','B1','B2','B3'}"""
    o = h1.o.values.astype(float); h = h1.h.values.astype(float)
    l = h1.l.values.astype(float); c = h1.c.values.astype(float)
    n = len(c)
    if n < 200:
        return []
    ma20 = ema(c, MA_N)
    atr_arr = atr14(h, l, c, ATR_N)
    adx_arr = adx14(h, l, c, ADX_N)
    e140 = ema(c, EMA_K)
    dir_raw = np.where(c > e140, 1, -1)
    htf_dir = np.concatenate([[0], dir_raw[:-1]])
    ok_l = htf_dir >= 0
    ok_s = htf_dir <= 0
    a1s_thr = int(cfg.get('a1s_thr', 80))
    hvol = h1[f'hvol{a1s_thr}'].values
    first = h1.sess_first.values
    last = h1.sess_last.values
    entry_mode = cfg.get('entry_mode', 'both_nm')
    extreme_adx = float(cfg.get('extreme_adx', 0.0))
    blocked_tds = cfg.get('blocked_tds', set())
    year = h1.year.values
    dtv = h1.dt.values
    mult = MULT[sym]; tick = TICK[sym]

    def cost1(px):     # 单边每手
        return (COMM_BP * 1e-4 * px + tick) * mult

    trades = []
    ph = []; pl = []
    state = 0
    entry_px = avg_px = e_atr = 0.0
    lots = 1.0
    entry_i = -1
    cooldown = 0
    peak = trough = 0.0
    be_done = False
    size = 1.0          # A1 仓位系数（0.5/1.0），作用于最终盈亏
    sl_eff = SL_ATR     # A1 止损距离系数
    entry_meta = {}

    for i in range(2, n):
        if h[i - 1] > h[i - 2] and h[i - 1] > h[i]:
            ph.append(i - 1)
        if l[i - 1] < l[i - 2] and l[i - 1] < l[i]:
            pl.append(i - 1)
        while ph and ph[0] < i - W_PIVOT:
            ph.pop(0)
        while pl and pl[0] < i - W_PIVOT:
            pl.pop(0)
        if cooldown > 0:
            cooldown -= 1

        # ---- 离场（close-only）----
        if state == 1:
            if c[i] > peak:
                peak = c[i]
            st = avg_px - sl_eff * e_atr
            t = peak - sl_eff * e_atr
            if t > st:
                st = t
            if (c[i] - entry_px) >= BE_R * e_atr:
                be_done = True
            if be_done and entry_px > st:
                st = entry_px
            if c[i] <= st:
                fill = c[i]
                raw = (fill - avg_px) * lots * mult * size
                cost = (cost1(avg_px) + cost1(fill)) * lots * size
                net = raw - cost
                trades.append(dict(sym=sym, dir=1, edt=entry_meta['edt'], xdt=dtv[i],
                                   P=avg_px, X=fill, lots=lots * size, e_atr=e_atr,
                                   net=net, gross=raw, r=net / (SL_ATR * e_atr * mult),
                                   year=year[i], etype=entry_meta['etype'], hvol=entry_meta['hvol']))
                state = 0; cooldown = COOLDOWN; be_done = False; lots = 1.0
        elif state == 2:
            if c[i] < trough:
                trough = c[i]
            st = avg_px + sl_eff * e_atr
            t = trough + sl_eff * e_atr
            if t < st:
                st = t
            if (entry_px - c[i]) >= BE_R * e_atr:
                be_done = True
            if be_done and entry_px < st:
                st = entry_px
            if c[i] >= st:
                fill = c[i]
                raw = (avg_px - fill) * lots * mult * size
                cost = (cost1(avg_px) + cost1(fill)) * lots * size
                net = raw - cost
                trades.append(dict(sym=sym, dir=-1, edt=entry_meta['edt'], xdt=dtv[i],
                                   P=avg_px, X=fill, lots=lots * size, e_atr=e_atr,
                                   net=net, gross=raw, r=net / (SL_ATR * e_atr * mult),
                                   year=year[i], etype=entry_meta['etype'], hvol=entry_meta['hvol']))
                state = 0; cooldown = COOLDOWN; be_done = False; lots = 1.0

        # ---- 进场 ----
        if state == 0 and cooldown == 0 and i >= ENTRY_MIN_I:
            if 'B2' in variant and first[i]:
                pass
            elif 'B3' in variant and last[i]:
                pass
            else:
                up = c[i] > ma20[i]
                dn = c[i] < ma20[i]
                cross_up = up and c[i - 1] < ma20[i - 1]
                cross_down = dn and c[i - 1] > ma20[i - 1]
                tl = l[i] <= ma20[i]
                ts = h[i] >= ma20[i]
                # use_sbull=False → 无收强/收弱要求
                s_tl = ok_l[i] and up and tl and cross_up
                s_ts = ok_s[i] and dn and ts and cross_down
                _hh10 = max(h[max(0, i - 10):i])
                _ll10 = min(l[max(0, i - 10):i])
                s_bkl = ok_l[i] and c[i] > _hh10 and c[i] > o[i]
                s_bks = ok_s[i] and c[i] < _ll10 and c[i] < o[i]
                if entry_mode == 'pullback':
                    s_bkl = s_bks = False
                # 斐波汇流（仅回踩支路）
                if s_tl:
                    leg = None
                    if pl:
                        sli = pl[-1]
                        if i - sli >= 2:
                            seg = h[sli:i + 1]
                            shi = sli + int(np.argmax(seg))
                            lg = float(seg.max()) - float(l[sli])
                            if lg > 0:
                                leg = (shi, lg)
                    if leg is None:
                        s_tl = False
                    else:
                        shp = float(h[leg[0]])
                        tol = FIB_TOL * atr_arr[i]
                        if not any(abs(float(l[i]) - (shp - r * leg[1])) <= tol for r in FIB_RATIOS):
                            s_tl = False
                if s_ts:
                    leg = None
                    if ph:
                        shj = ph[-1]
                        if i - shj >= 2:
                            seg = l[shj:i + 1]
                            slj = shj + int(np.argmin(seg))
                            lg = float(h[shj]) - float(seg.min())
                            if lg > 0:
                                leg = (slj, lg)
                    if leg is None:
                        s_ts = False
                    else:
                        slp = float(l[leg[0]])
                        tol = FIB_TOL * atr_arr[i]
                        if not any(abs(float(h[i]) - (slp + r * leg[1])) <= tol for r in FIB_RATIOS):
                            s_ts = False
                # ADX 门控（上一根）
                if adx_arr[i - 1] < ADX_MIN:
                    s_tl = s_ts = s_bkl = s_bks = False

                sig = 1 if (s_tl or s_bkl) else (-1 if (s_ts or s_bks) else 0)
                if sig != 0 and 'A1S' in variant and hvol[i]:
                    sig = 0
                if sig != 0 and blocked_tds and h1.td.values[i] in blocked_tds:
                    sig = 0
                # P7：极端收盘信号要求 ADX 更强确认（不延迟入场，改为过滤弱信号）
                if sig != 0 and extreme_adx > 0:
                    rng = h[i] - l[i]
                    clv = (c[i] - l[i]) / rng if rng > 0 else 0.5
                    if (sig > 0 and clv >= 0.8) or (sig < 0 and clv <= 0.2):
                        if adx_arr[i - 1] < extreme_adx:
                            sig = 0
                if sig != 0:
                    # A1：高波日新开仓 → 仓位减半 + 止损放宽 1.34×
                    size = 0.5 if ('A1' in variant and hvol[i]) else 1.0
                    sl_eff = SL_ATR * (1.34 if ('A1' in variant and hvol[i]) else 1.0)
                    # B1：极端收盘延迟（clv 0-1 口径）
                    fill = c[i]
                    ei = i
                    if 'B1' in variant and i + 1 < n:
                        rng = h[i] - l[i]
                        clv = (c[i] - l[i]) / rng if rng > 0 else 0.5
                        a0 = atr_arr[i]
                        if sig > 0 and clv >= 0.8:
                            lim = c[i] - 0.25 * a0
                            fill = lim if l[i + 1] <= lim else c[i + 1]
                            ei = i + 1
                        elif sig < 0 and clv <= 0.2:
                            lim = c[i] + 0.25 * a0
                            fill = lim if h[i + 1] >= lim else c[i + 1]
                            ei = i + 1
                    state = 1 if sig > 0 else 2
                    entry_px = avg_px = fill
                    lots = 1.0
                    entry_i = ei
                    e_atr = atr_arr[i]
                    peak = trough = fill
                    be_done = False
                    entry_meta = dict(edt=dtv[ei], etype='pb' if (s_tl or s_ts) else 'bk',
                                      hvol=bool(hvol[i]))
                    continue
        # ---- P1 加码 ----
        elif ADD_MAX_LOTS >= 2 and state != 0 and lots < ADD_MAX_LOTS:
            if state == 1:
                _hh = max(h[entry_i:i]) if i > entry_i else h[entry_i]
                if c[i] > _hh and (c[i] - entry_px) >= lots * ADD_THR * e_atr:
                    avg_px = (avg_px * lots + float(c[i])) / (lots + 1.0)
                    lots += 1.0
            elif state == 2:
                _ll = min(l[entry_i:i]) if i > entry_i else l[entry_i]
                if c[i] < _ll and (entry_px - c[i]) >= lots * ADD_THR * e_atr:
                    avg_px = (avg_px * lots + float(c[i])) / (lots + 1.0)
                    lots += 1.0

    # 数据末尾强平
    if state != 0:
        fill = c[-1]
        raw = ((fill - avg_px) if state == 1 else (avg_px - fill)) * lots * mult * size
        cost = (cost1(avg_px) + cost1(fill)) * lots * size
        net = raw - cost
        trades.append(dict(sym=sym, dir=state, edt=entry_meta['edt'], xdt=dtv[-1],
                           P=avg_px, X=fill, lots=lots * size, e_atr=e_atr, net=net,
                           gross=raw, r=net / (SL_ATR * e_atr * mult), year=year[-1],
                           etype=entry_meta['etype'], hvol=entry_meta['hvol']))
    return trades


def apply_a2(tb):
    """A2 板块簇同时持仓 ≤2：全局按入场时间贪心接受。"""
    allt = sorted([t for ts in tb.values() for t in ts], key=lambda t: (t['edt'], t['sym']))
    open_until = {}
    out = []
    for t in allt:
        cl = CLUSTER.get(t['sym'])
        if cl is None:
            out.append(t)
            continue
        lst = [x for x in open_until.get(cl, []) if x > t['edt']]
        if len(lst) >= 2:
            continue
        lst.append(t['xdt'])
        open_until[cl] = lst
        out.append(t)
    return out


def metrics(trades, label):
    if not trades:
        return dict(label=label, n=0)
    df = pd.DataFrame(trades).sort_values('xdt').reset_index(drop=True)
    eq = df.net.cumsum()
    mdd = float((eq - eq.cummax()).min())
    n = len(df)
    gp = df.loc[df.net > 0, 'net'].sum()
    gl = -df.loc[df.net < 0, 'net'].sum()
    out = dict(label=label, n=n, win=float((df.net > 0).mean()),
               gross_yuan=float(df.gross.sum()),
               pf=float(gp / gl) if gl > 0 else np.nan,
               avg_r=float(df.r.mean()), tot_r=float(df.r.sum()),
               net_yuan=float(df.net.sum()), mdd_yuan=mdd,
               mar=float(df.net.sum() / abs(mdd)) if mdd < 0 else np.nan)
    for tag, m in [('train', df.year <= 2025), ('holdout', df.year >= 2026)]:
        sub = df[m]
        if len(sub):
            eq2 = sub.net.cumsum()
            out[tag] = dict(n=int(len(sub)), net_yuan=float(sub.net.sum()),
                            win=float((sub.net > 0).mean()),
                            pf=float(sub.loc[sub.net > 0, 'net'].sum() /
                                     max(1e-9, -sub.loc[sub.net < 0, 'net'].sum())),
                            mdd_yuan=float((eq2 - eq2.cummax()).min()))
    return out


def main():
    variants = ['BASE', 'A1', 'A1S', 'A2', 'B1', 'B2', 'B3', 'COMBO', 'COMBO2',
                'P2', 'P3', 'P4', 'P6', 'P7', 'V4', 'V4NP', 'V4NP2', 'V4NP3', 'V4NP5',
                'THR50', 'THR60', 'THR70', 'THR75', 'THR85', 'THR90', 'THR95']
    P5_POOL = ['EB', 'IF']           # P4 之前证据（陈旧门，holdout 有害，仅存档）
    P5_POST_P4 = ['EB', 'SC', 'FG', 'SA', 'IF', 'V']   # P4 之后重估 gross/cost ≥1.5
    data = {}
    for sym in SYMS:
        try:
            data[sym] = build_h1(sym)
            print(f'{sym}: {len(data[sym])} hourly bars', flush=True)
        except Exception as e:
            print(f'{sym}: SKIP {type(e).__name__} {e}', flush=True)

    # ---- V4 逐项配置（证据来自 BASE 交易明细，见报告）----
    shock = cluster_shock_days(data)
    cfg_shock = {s: {'blocked_tds': shock.get(CLUSTER.get(s), set())} for s in data}
    cfg_p4 = {s: ({} if s == 'IF' else {'entry_mode': 'pullback'}) for s in data}
    cfg_p7 = {s: {'extreme_adx': 20.0} for s in data}
    cfg_thr = lambda q: {s: {'a1s_thr': q} for s in data}

    def merge(*ds):
        out = {s: {} for s in data}
        for d in ds:
            for s, c in d.items():
                out[s].update(c)
        return out

    def run_variant(v):
        """返回 (metrics, trades)。"""
        flags, pool, cfg = set(), list(data), {}
        if v == 'BASE':
            pass
        elif v == 'A1':
            flags = {'A1'}
        elif v == 'A1S':
            flags = {'A1S'}
        elif v == 'A2':
            flags = {'A2'}
        elif v == 'B1':
            flags = {'B1'}
        elif v == 'B2':
            flags = {'B2'}
        elif v == 'B3':
            flags = {'B3'}
        elif v == 'COMBO':
            flags = {'A1', 'A2', 'B1', 'B2', 'B3'}
        elif v == 'COMBO2':
            flags = {'A1S', 'A2', 'B2', 'B3'}
        elif v.startswith('THR'):
            flags = {'A1S'}
            cfg = cfg_thr(int(v[3:]))
        elif v == 'P2':                       # 高波日不开仓（单项）
            flags = {'A1S'}
        elif v == 'P3':                       # 簇上限 + 簇冲击日封禁
            flags = {'A2'}
            cfg = cfg_shock
        elif v == 'P4':                       # 化工只回踩、IF 保留突破
            cfg = cfg_p4
        elif v == 'P6':                       # 尾盘根不开仓
            flags = {'B3'}
        elif v == 'P7':                       # 极端收盘 ADX≥20 确认
            cfg = cfg_p7
        elif v == 'V4':                      # 全项融合 + P5 品种池
            flags = {'A1S', 'A2', 'B3'}
            cfg = merge(cfg_shock, cfg_p4, cfg_p7)
            pool = [s for s in data if s in P5_POOL]
        elif v == 'V4NP':                    # 全项融合，全品种（含 P7）
            flags = {'A1S', 'A2', 'B3'}
            cfg = merge(cfg_shock, cfg_p4, cfg_p7)
        elif v == 'V4NP2':                   # 去 P7（实测有害），全品种
            flags = {'A1S', 'A2', 'B3'}
            cfg = merge(cfg_shock, cfg_p4)
        elif v == 'V4NP3':                   # V4NP2 + 高波阈值 70
            flags = {'A1S', 'A2', 'B3'}
            cfg = merge(cfg_shock, cfg_p4, cfg_thr(70))
        elif v == 'V4NP5':                   # V4NP2 + P5 门（P4 之后重估）
            flags = {'A1S', 'A2', 'B3'}
            cfg = merge(cfg_shock, cfg_p4)
            pool = [s for s in data if s in P5_POST_P4]
        else:
            raise ValueError(v)
        tb = {sym: run_symbol(sym, data[sym], flags - {'A2'}, cfg.get(sym, {}))
              for sym in pool}
        trades = apply_a2(tb) if 'A2' in flags else [t for ts in tb.values() for t in ts]
        return metrics(trades, v), trades

    results = {}
    for v in variants:
        m, trades = run_variant(v)
        results[v] = m
        results[v]['per_sym'] = {s: metrics([t for t in trades if t['sym'] == s], s)
                                 for s in sorted({t['sym'] for t in trades})}
        print(v, json.dumps({k: m.get(k) for k in
                             ['n', 'win', 'pf', 'net_yuan', 'mdd_yuan', 'mar']},
                            ensure_ascii=False, default=str), flush=True)
        if v in ('BASE', 'V4', 'V4NP', 'V4NP2', 'V4NP3', 'V4NP5', 'COMBO2'):
            pd.DataFrame(trades).to_csv(f'trades_{v}.csv', index=False, encoding='utf-8-sig')
    json.dump(results, open('results_fusion_bt.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('saved results_fusion_bt.json')


if __name__ == '__main__':
    main()
