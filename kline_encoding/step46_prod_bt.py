# -*- coding: utf-8 -*-
"""step46 融合策略 V3.4 在生产库 59 品种数据上的全量回测 + MACD/KDJ/量价/持仓因子融合门控

数据：prod50/（云端 qhyc-timescaledb 导出，2026-10-18）
  bar_60m_ba.csv.gz   后复权 60m（信号与盈亏口径，bucket=bar 起始小时，会话末有微量 stub）
  bar_60m_raw.csv.gz  原始价 60m（成本/保证金口径）
  daily_bar.csv.gz    主连日线（hvol 体制 / 簇冲击 / 交易日历）
  dim_variety.csv     tick / multiplier / 保证金
  dim_trading_cost.csv 费率字典（FIXED 元/手 | PCT ‱ | FREE + slip_ticks，OPEN/CLOSE_YEST/CLOSE_TODAY）
  factor_value.csv.gz 26 个日频因子（member_net_z 等，available_at 即滞后纪律）

新门控（证据见桌面「融合」文档与第二章研究）：
  M1a_pb / M1a_all  MACD 柱与方向一致性（金叉遇绿柱 −12.5pp / 死叉遇红柱 +12.6pp，59 品种 48/49 稳健）
  M1b               精确版「金叉遇绿柱」：3 根内 KDJ 金叉且 hist<0 → 禁多（镜像禁空）
  M2                KDJ 位置门（K>80 禁突破多 / K<20 禁突破空）——全市场证据≈0，作对照
  M3                放量突破门：突破支路要求当根量 ≥ 1.5×前 20 根中位量
  M5 / M5b          会员净持仓 |z|≥1.0（严格 available_at 滞后）；M5b 加方向一致
组合：V4NP2（P2+P3+P4+P6）± 各 M；V5 = V4NP2+M1a_pb+M3+M5
"""
import json
import numpy as np
import pandas as pd

DATA = 'prod50'
MA_N, ATR_N, EMA_K = 20, 14, 140
SL_ATR, TRAIL_ATR, BE_R = 1.75, 2.0, 1.0   # V5X 正式版(V5X.2, 2026-10-22 自进化重锁·无bug代码最优): SL1.75/吊灯2.0/保本1.0
W_PIVOT, COOLDOWN = 60, 7                # COOLDOWN 1->7(离场后冷却更充分，抑制过拟合噪声)
ADX_N, ADX_MIN = 14, 15.0
FIB_RATIOS, FIB_TOL = (0.382, 0.5, 0.618), 0.5
ADD_MAX_LOTS, ADD_THR = 2, 1.5           # ADD_THR 1.0->1.5(加仓门槛更严)
ENTRY_MIN_I = 30
WARMUP_DAYS = 200          # 对齐生产（sym_cut 前事件丢弃）
EXCLUDE = set()            # CJ888 已物理移出所有 L1 表(2026-10-22)，保留空集合作未来坏值护栏

CLUSTER = {
    'TA': '聚酯', 'PX': '聚酯', 'PF': '聚酯', 'PR': '聚酯',
    'PP': '聚烯烃', 'L': '聚烯烃', 'V': '聚烯烃', 'EB': '聚烯烃',
    'FG': '两碱', 'SA': '两碱', 'SH': '两碱',
    'MA': '醇醚', 'EG': '醇醚', 'UR': '醇醚',
    'RB': '黑色', 'HC': '黑色', 'I': '黑色', 'J': '黑色', 'JM': '黑色',
    'SF': '黑色', 'SM': '黑色', 'SS': '黑色',
    'P': '油脂', 'Y': '油脂', 'OI': '油脂',
    'M': '粕类', 'RM': '粕类',
    'CU': '有色', 'AL': '有色', 'ZN': '有色', 'PB': '有色', 'NI': '有色', 'SN': '有色', 'AO': '有色',
    'AU': '贵金属', 'AG': '贵金属',
    'SC': '能源', 'FU': '能源', 'LU': '能源', 'BU': '能源', 'PG': '能源',
    'CF': '软商品', 'SR': '软商品', 'AP': '软商品', 'JD': '软商品', 'LH': '软商品', 'PK': '软商品',
    'A': '谷物', 'B': '谷物', 'C': '谷物', 'CS': '谷物',
    'RU': '橡胶', 'BR': '橡胶',
    'LC': '新能源', 'SI': '新能源', 'PS': '新能源',
    'SP': '纸浆', 'EC': '航运',
}
# P4「只回踩」适用范围：第二章证据所在的化工链条（聚酯/聚烯烃/两碱/醇醚）
P4_SECTORS = {'聚酯', '聚烯烃', '两碱', '醇醚'}
# P4E 扩展口径：全部能化+能源
P4E_SECTORS = P4_SECTORS | {'能源', '橡胶', '纸浆'}


# ---------------- 指标 ----------------
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


def macd_hist(c):
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    return 2.0 * (dif - dea)


def kdj(h, l, c, n=9):
    hh = pd.Series(h).rolling(n, min_periods=1).max().to_numpy()
    ll = pd.Series(l).rolling(n, min_periods=1).min().to_numpy()
    rsv = np.where(hh > ll, (c - ll) / (hh - ll) * 100.0, 50.0)
    k = pd.Series(rsv).ewm(alpha=1 / 3, adjust=False).mean().to_numpy()
    d = pd.Series(k).ewm(alpha=1 / 3, adjust=False).mean().to_numpy()
    return k, d, 3 * k - 2 * d


# ---------------- 数据装载 ----------------
def load_all():
    ba = pd.read_csv(f'{DATA}/bar_60m_ba.csv.gz', parse_dates=['ts'])
    raw = pd.read_csv(f'{DATA}/bar_60m_raw.csv.gz', parse_dates=['ts'])
    dd = pd.read_csv(f'{DATA}/daily_bar.csv.gz', parse_dates=['trade_date'])
    var = pd.read_csv(f'{DATA}/dim_variety.csv')
    cost = pd.read_csv(f'{DATA}/dim_trading_cost.csv')
    fac = pd.read_csv(f'{DATA}/factor_value.csv.gz', parse_dates=['trade_date', 'available_at'])
    return ba, raw, dd, var, cost, fac


def build_cost_fn(cost, var):
    """返回 {品种: (fixed_open, pct_open, fixed_ct, pct_ct, fixed_cy, pct_cy, slip_yuan)}"""
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    mult = dict(zip(var.variety_code, var.multiplier.astype(float)))
    out = {}
    cur = cost[cost.effective_to.isna()]
    for vc, g in cur.groupby('variety_code'):
        g_all = g[g.scope_kind == 'ALL']
        g = g_all if len(g_all) else g
        g = g[g.instrument_kind == 'FUTURE'] if (g.instrument_kind == 'FUTURE').any() else g
        row = {}
        for act in ['OPEN', 'CLOSE_TODAY', 'CLOSE_YEST']:
            gg = g[g.action == act]
            if not len(gg):
                gg = g[g.action == 'OPEN']
            r = gg.sort_values('effective_from').iloc[-1]
            fx = float(r.fee_value) if r.fee_type == 'FIXED' else 0.0
            pc = float(r.fee_value) / 10000.0 if r.fee_type == 'PCT' else 0.0
            row[act] = (fx, pc)
        slip = float(g.slip_ticks.max()) * tick.get(vc, 1.0)
        out[vc] = (row, slip, mult.get(vc, 1.0))
    return out


def td_assign(ts, cal):
    """bucket 时间 → 交易日：夜盘（>=18:00）归次一交易日；日盘归当日。"""
    d = ts.dt.normalize()
    night = ts.dt.hour.values >= 18
    calv = cal.values
    dv = d.values
    td = np.empty(len(ts), dtype='datetime64[ns]')
    pos_day = np.searchsorted(calv, dv)
    pos_day = np.minimum(pos_day, len(calv) - 1)
    td[~night] = calv[pos_day[~night]]
    pos_n = np.searchsorted(calv, dv, side='right')
    pos_n = np.minimum(pos_n, len(calv) - 1)
    td[night] = calv[pos_n[night]]
    return td


def build_sym(sym, ba, raw, dd, var):
    """单品种数据包：60m（复权+原始）+ 日线体制 + 会话首末根。"""
    b = ba[ba.symbol == sym].sort_values('ts').reset_index(drop=True)
    if len(b) < 1500:
        return None
    r = raw[raw.symbol == sym].sort_values('ts').reset_index(drop=True)
    b['raw_o'] = r.open.values
    b['raw_c'] = r.close.values
    dly = dd[dd.symbol == sym].sort_values('trade_date').reset_index(drop=True)
    cal = dly.trade_date
    b['td'] = td_assign(b.ts, cal)
    b['hour'] = b.ts.dt.hour
    b['year'] = b.ts.dt.year
    # 会话首末根（末根=收盘前最后一个完整小时：日盘 14、夜盘 = 最大夜盘小时-1）
    night_hours = sorted(b.loc[b.hour >= 18, 'hour'].unique())
    night_tail = (max(night_hours) - 1) if night_hours else None
    first_b = (b.hour == 9) | (b.hour == 21)
    last_b = (b.hour == 14) | ((b.hour == night_tail) if night_tail is not None else False)
    # SC 等 01:00/02:30 收盘：夜盘小时跨 0 点
    late = sorted(b.loc[b.hour <= 3, 'hour'].unique())
    if late:
        last_b = last_b | (b.hour == max(late) - 1 if max(late) >= 1 else False)
    b['sess_first'] = first_b.values
    b['sess_last'] = last_b.values
    # 日线体制
    dly['pc'] = dly.close.shift(1)
    dly['amp'] = (dly.high - dly.low) / dly.pc
    for q in [70, 80]:
        dly[f'q{q}'] = dly.amp.rolling(250, min_periods=60).quantile(q / 100.0)
        dly[f'hvol{q}'] = (dly.amp > dly[f'q{q}']).shift(1).fillna(False)
    hv = dly.set_index('trade_date')
    b['hvol'] = b.td.map(hv.hvol80).fillna(False).values.astype(bool)
    b['hvol70'] = b.td.map(hv.hvol70).fillna(False).values.astype(bool)
    b['lret_d'] = b.td.map(np.log(dly.close / dly.close.shift(1)).set_axis(dly.trade_date)).values
    # 预热：上市 200 个自然日后才允许事件
    b['allow'] = (b.ts >= (b.ts.iloc[0] + pd.Timedelta(days=WARMUP_DAYS))).values
    # 指标（复权价）
    c = b.close.values.astype(float)
    h = b.high.values.astype(float)
    l = b.low.values.astype(float)
    b['mh'] = macd_hist(c)
    k, d2, j = kdj(h, l, c)
    b['kdj_k'] = k
    b['kdj_d'] = d2
    v = b.volume.values.astype(float)
    vmed = pd.Series(v).rolling(20, min_periods=5).median().shift(1).to_numpy()
    b['vol_ratio'] = np.where(vmed > 0, v / np.maximum(vmed, 1e-9), np.nan)
    return b


def attach_member_z(data, fac):
    """member_net_z 严格滞后：bar 只能用 available_at <= 当根交易日 的因子值。"""
    mz = fac[fac.factor_id == 'member_net_z'][['symbol', 'trade_date', 'z_value', 'available_at']]
    mz = mz.sort_values(['symbol', 'available_at'])
    for sym, b in data.items():
        g = mz[mz.symbol == sym]
        if not len(g):
            b['member_z'] = np.full(len(b), np.nan)
            continue
        tds = b.td.values
        av = g.available_at.values.astype('datetime64[D]')
        td_d = tds.astype('datetime64[D]')
        idx = np.searchsorted(av, td_d, side='right') - 1
        z = np.full(len(b), np.nan)
        ok = idx >= 0
        z[ok] = g.z_value.values[idx[ok]]
        b['member_z'] = z
    return data


def cluster_shock_days(data):
    daily = {}
    for sym, b in data.items():
        cl = CLUSTER.get(sym)
        if cl is None:
            continue
        dd = b.groupby('td', sort=True).agg(c=('close', 'last')).reset_index()
        dd['lret'] = np.log(dd.c / dd.c.shift(1))
        daily.setdefault(cl, []).append(dd.set_index('td').lret)
    out = {}
    for cl, serlist in daily.items():
        rr = pd.concat(serlist, axis=1).mean(axis=1)
        sd = rr.rolling(250, min_periods=60).std()
        shock = (rr.abs() > 3 * sd).fillna(False)
        out[cl] = set(rr.index[shock.values])
    return out


# ---------------- 引擎（逐行对齐 step43 / walk_fusion_states V3.4）----------------
def run_symbol(sym, b, cfgs, flags):
    o = b.open.values.astype(float); h = b.high.values.astype(float)
    l = b.low.values.astype(float); c = b.close.values.astype(float)
    raw_o = b.raw_o.values.astype(float); raw_c = b.raw_c.values.astype(float)
    n = len(c)
    ma20 = ema(c, MA_N)
    atr_arr = atr14(h, l, c, ATR_N)
    adx_arr = adx14(h, l, c, ADX_N)
    e140 = ema(c, EMA_K)
    dir_raw = np.where(c > e140, 1, -1)
    htf_dir = np.concatenate([[0], dir_raw[:-1]])
    ok_l = htf_dir >= 0
    ok_s = htf_dir <= 0
    hist = b.mh.values
    kk = b.kdj_k.values; kd = b.kdj_d.values
    volr = b.vol_ratio.values
    mz = b.member_z.values
    hvol = b.hvol.values
    allow = b.allow.values
    first = b.sess_first; last = b.sess_last
    tds = b.td.values
    year = b.year.values
    dtv = b.ts.values
    (fee_row, slip, mult) = cfgs['cost']
    tick = cfgs['tick']
    entry_mode = cfgs.get('entry_mode', 'both_nm')
    blocked_tds = cfgs.get('blocked_tds', set())

    def fee(act, px_raw):
        fx, pc = fee_row[act]
        return fx + pc * px_raw * mult

    def cost_round(px_e_raw, px_x_raw, same_day):
        cx = fee('CLOSE_TODAY' if same_day else 'CLOSE_YEST', px_x_raw)
        return fee('OPEN', px_e_raw) + cx + 2 * slip * mult

    trades = []
    ph = []; pl = []
    state = 0
    entry_px = avg_px = e_atr = 0.0
    lots = 1.0
    entry_i = -1
    cooldown = 0
    peak = trough = 0.0
    be_done = False
    entry_meta = {}
    entry_td = None

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

        # ---- 离场 ----
        if state == 1:
            if c[i] > peak:
                peak = c[i]
            st = avg_px - SL_ATR * e_atr
            t = peak - TRAIL_ATR * e_atr
            if t > st:
                st = t
            if (c[i] - entry_px) >= BE_R * e_atr:
                be_done = True
            if be_done and entry_px > st:
                st = entry_px
            if c[i] <= st:
                fill = c[i]
                raw = (fill - avg_px) * lots * mult
                cost = cost_round(cfgs['entry_raw'], raw_c[i], tds[i] == entry_td) * lots
                net = raw - cost
                trades.append(dict(sym=sym, dir=1, edt=entry_meta['edt'], xdt=dtv[i],
                                   P=avg_px, X=fill, lots=lots, e_atr=e_atr,
                                   net=net, gross=raw, r=net / (SL_ATR * e_atr * mult),
                                   year=year[i], etype=entry_meta['etype'], hvol=entry_meta['hvol']))
                state = 0; cooldown = COOLDOWN; be_done = False; lots = 1.0
        elif state == 2:
            if c[i] < trough:
                trough = c[i]
            st = avg_px + SL_ATR * e_atr
            t = trough + TRAIL_ATR * e_atr
            if t < st:
                st = t
            if (entry_px - c[i]) >= BE_R * e_atr:
                be_done = True
            if be_done and entry_px < st:
                st = entry_px
            if c[i] >= st:
                fill = c[i]
                raw = (avg_px - fill) * lots * mult
                cost = cost_round(cfgs['entry_raw'], raw_c[i], tds[i] == entry_td) * lots
                net = raw - cost
                trades.append(dict(sym=sym, dir=-1, edt=entry_meta['edt'], xdt=dtv[i],
                                   P=avg_px, X=fill, lots=lots, e_atr=e_atr,
                                   net=net, gross=raw, r=net / (SL_ATR * e_atr * mult),
                                   year=year[i], etype=entry_meta['etype'], hvol=entry_meta['hvol']))
                state = 0; cooldown = COOLDOWN; be_done = False; lots = 1.0

        # ---- 进场 ----
        if state == 0 and cooldown == 0 and i >= ENTRY_MIN_I and allow[i]:
            if 'B2' in flags and first[i]:
                pass
            elif 'B3' in flags and last[i]:
                pass
            else:
                up = c[i] > ma20[i]
                dn = c[i] < ma20[i]
                cross_up = up and c[i - 1] < ma20[i - 1]
                cross_down = dn and c[i - 1] > ma20[i - 1]
                tl = l[i] <= ma20[i]
                ts = h[i] >= ma20[i]
                s_tl = ok_l[i] and up and tl and cross_up
                s_ts = ok_s[i] and dn and ts and cross_down
                _hh10 = max(h[max(0, i - 10):i])
                _ll10 = min(l[max(0, i - 10):i])
                s_bkl = ok_l[i] and c[i] > _hh10 and c[i] > o[i]
                s_bks = ok_s[i] and c[i] < _ll10 and c[i] < o[i]
                if entry_mode == 'pullback':
                    s_bkl = s_bks = False
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
                if adx_arr[i - 1] < ADX_MIN:
                    s_tl = s_ts = s_bkl = s_bks = False

                sig = 1 if (s_tl or s_bkl) else (-1 if (s_ts or s_bks) else 0)
                # ---- 研究门控 ----
                if sig != 0 and 'A1S' in flags and hvol[i]:
                    sig = 0
                if sig != 0 and blocked_tds and tds[i] in blocked_tds:
                    sig = 0
                # M1a：MACD 柱方向一致门（pb=仅回踩支路 / all=全部支路）
                if sig != 0 and ('M1a_pb' in flags or 'M1a_all' in flags):
                    is_pb = (s_tl or s_ts)
                    if is_pb or 'M1a_all' in flags:
                        if (sig > 0 and hist[i - 1] < 0) or (sig < 0 and hist[i - 1] > 0):
                            sig = 0
                # M1b：精确「金叉遇绿柱 / 死叉遇红柱」
                if sig != 0 and 'M1b' in flags:
                    lo3 = slice(max(0, i - 3), i)
                    gcx = np.any((kk[lo3] > kd[lo3]) & (kk[np.maximum(0, np.arange(max(0, i - 3), i)) - 1] <= kd[np.maximum(0, np.arange(max(0, i - 3), i)) - 1])) if i >= 1 else False
                    dcx = np.any((kk[lo3] < kd[lo3]) & (kk[np.maximum(0, np.arange(max(0, i - 3), i)) - 1] >= kd[np.maximum(0, np.arange(max(0, i - 3), i)) - 1])) if i >= 1 else False
                    if sig > 0 and gcx and hist[i - 1] < 0:
                        sig = 0
                    if sig < 0 and dcx and hist[i - 1] > 0:
                        sig = 0
                # M2：KDJ 位置门（对照，弱证据）
                if sig != 0 and 'M2' in flags:
                    if sig > 0 and kk[i - 1] > 80:
                        sig = 0
                    if sig < 0 and kk[i - 1] < 20:
                        sig = 0
                # M3：放量突破门（仅突破支路）
                if sig != 0 and 'M3' in flags:
                    is_bk = (s_bkl or s_bks)
                    if is_bk and not (volr[i] >= 1.5):
                        sig = 0
                # M1a_rev 反向对照：hist 同号反而禁
                if sig != 0 and 'M1a_rev' in flags:
                    if (sig > 0 and hist[i - 1] > 0) or (sig < 0 and hist[i - 1] < 0):
                        sig = 0
                # M2_rev 反向对照：低位禁多/高位禁空
                if sig != 0 and 'M2_rev' in flags:
                    if sig > 0 and kk[i - 1] < 20:
                        sig = 0
                    if sig < 0 and kk[i - 1] > 80:
                        sig = 0
                # M5_rev 反向对照：只在弱 z 时开仓
                if sig != 0 and 'M5_rev' in flags:
                    z = mz[i]
                    if not np.isnan(z) and abs(z) >= 1.0:
                        sig = 0
                # M5：会员净持仓强度门；M5b 加方向一致；M5_05/M5_15 阈值敏感性
                if sig != 0 and ('M5' in flags or 'M5b' in flags or 'M5_05' in flags or 'M5_15' in flags):
                    z = mz[i]
                    thr = 0.5 if 'M5_05' in flags else (1.5 if 'M5_15' in flags else 1.0)
                    if not np.isnan(z):
                        if abs(z) < thr:
                            sig = 0
                        elif 'M5b' in flags and np.sign(z) != sig:
                            sig = 0
                if sig != 0:
                    state = 1 if sig > 0 else 2
                    entry_px = avg_px = c[i]
                    cfgs['entry_raw'] = raw_c[i]
                    lots = 1.0
                    entry_i = i
                    e_atr = atr_arr[i]
                    peak = trough = c[i]
                    be_done = False
                    entry_td = tds[i]
                    entry_meta = dict(edt=dtv[i], etype='pb' if (s_tl or s_ts) else 'bk',
                                      hvol=bool(hvol[i]))
                    continue
        # ---- 加码 ----
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

    if state != 0:
        fill = c[-1]
        raw = ((fill - avg_px) if state == 1 else (avg_px - fill)) * lots * mult
        cost = cost_round(cfgs['entry_raw'], raw_c[-1], tds[-1] == entry_td) * lots
        net = raw - cost
        trades.append(dict(sym=sym, dir=state, edt=entry_meta['edt'], xdt=dtv[-1],
                           P=avg_px, X=fill, lots=lots, e_atr=e_atr, net=net,
                           gross=raw, r=net / (SL_ATR * e_atr * mult), year=year[-1],
                           etype=entry_meta['etype'], hvol=entry_meta['hvol']))
    return trades


def apply_a2(tb):
    allt = sorted([t for ts in tb.values() for t in ts], key=lambda t: (t['edt'], t['sym']))
    open_until = {}
    out = []
    for t in allt:
        cl = CLUSTER.get(t['sym'][:-3] if t['sym'].endswith('888') else t['sym'])
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
    for tag, m in [('train', df.year <= 2025), ('holdout', df.year >= 2026),
                   ('oos21', df.year >= 2021)]:
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
    ba, raw, dd, var, cost, fac = load_all()
    costmap = build_cost_fn(cost, var)
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    syms = sorted(s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in EXCLUDE)
    print(f'universe: {len(syms)} syms with 60m data + cost', flush=True)

    data = {}
    for s in syms:
        try:
            b = build_sym(s, ba, raw, dd, var)
            if b is not None:
                data[s] = b
        except Exception as e:
            print(f'{s}: SKIP {type(e).__name__} {e}', flush=True)
    print(f'loaded {len(data)} syms', flush=True)
    data = attach_member_z(data, fac)
    shock = cluster_shock_days(data)

    def cfg_for(sym, entry_scope=None):
        vc = sym[:-3]
        cl = CLUSTER.get(vc)
        cfg = {'cost': costmap[vc], 'tick': tick.get(vc, 1.0),
               'blocked_tds': shock.get(cl, set()) if cl else set()}
        if entry_scope and cl in entry_scope:
            cfg['entry_mode'] = 'pullback'
        return cfg

    def run_variant(v):
        flags, pool, scope = set(), list(data), None
        if v == 'BASE':
            pass
        elif v == 'P2':
            flags = {'A1S'}
        elif v == 'P3':
            flags = {'A2'}
        elif v == 'P4':
            scope = P4_SECTORS
        elif v == 'P4E':
            scope = P4E_SECTORS
        elif v == 'P6':
            flags = {'B3'}
        elif v == 'V4NP2':
            flags = {'A1S', 'A2', 'B3'}
            scope = P4_SECTORS
        elif v == 'M1a_rev':                # 反向对照：要求 hist 反向（应显著更差）
            flags = {'M1a_rev'}
        elif v == 'M2_rev':
            flags = {'M2_rev'}
        elif v == 'M5_rev':                 # 反向对照：只在 |z|<1.0 时开仓
            flags = {'M5_rev'}
        elif v.startswith('M'):
            flags = {v}
        elif v == 'V4_M1':
            flags = {'A1S', 'A2', 'B3', 'M1a_pb'}
            scope = P4_SECTORS
        elif v == 'V4_M5':
            flags = {'A1S', 'A2', 'B3', 'M5'}
            scope = P4_SECTORS
        elif v == 'V4_M1M5':
            flags = {'A1S', 'A2', 'B3', 'M1a_pb', 'M5'}
            scope = P4_SECTORS
        elif v == 'V4_M3':
            flags = {'A1S', 'A2', 'B3', 'M3'}
            scope = P4_SECTORS
        elif v == 'V5':
            flags = {'A1S', 'A2', 'B3', 'M1a_pb', 'M3', 'M5'}
            scope = P4_SECTORS
        elif v == 'V4X':                      # P2+P4+P6（不含簇控）
            flags = {'A1S', 'B3'}
            scope = P4_SECTORS
        elif v == 'V4X_M1':
            flags = {'A1S', 'B3', 'M1a_pb'}
            scope = P4_SECTORS
        elif v == 'V4X_M5':
            flags = {'A1S', 'B3', 'M5'}
            scope = P4_SECTORS
        elif v == 'V5X':                      # P2+P4+P6+M1a_pb+M5（终版候选）
            flags = {'A1S', 'B3', 'M1a_pb', 'M5'}
            scope = P4_SECTORS
        else:
            raise ValueError(v)
        tb = {sym: run_symbol(sym, data[sym], cfg_for(sym, scope), flags - {'A2'})
              for sym in pool}
        trades = apply_a2(tb) if 'A2' in flags else [t for ts in tb.values() for t in ts]
        return metrics(trades, v), trades

    import sys
    variants = ['BASE', 'P2', 'P3', 'P4', 'P4E', 'P6', 'V4NP2',
                'M1a_pb', 'M1a_all', 'M1b', 'M2', 'M3', 'M5', 'M5b',
                'M1a_rev', 'M2_rev', 'M5_rev',
                'V4_M1', 'V4_M5', 'V4_M1M5', 'V4_M3', 'V5']
    if len(sys.argv) > 1:
        variants = sys.argv[1:]
    try:
        results = json.load(open('results_step46.json', encoding='utf-8'))
    except Exception:
        results = {}
    for v in variants:
        m, trades = run_variant(v)
        results[v] = m
        results[v]['per_sym'] = {s: metrics([t for t in trades if t['sym'] == s], s)
                                 for s in sorted({t['sym'] for t in trades})}
        print(v, json.dumps({k: (round(x, 4) if isinstance(x, float) else x)
                             for k, x in m.items() if k in ['n', 'win', 'pf', 'net_yuan', 'mdd_yuan', 'mar']},
                            ensure_ascii=False, default=str), flush=True)
        if v in ('BASE', 'V4NP2', 'V5', 'V4_M1M5', 'M5', 'M5b', 'M1a_pb', 'M1a_all', 'M2', 'M5_rev', 'M1a_rev', 'V4X', 'V5X', 'V4X_M1', 'V4X_M5', 'M5_05', 'M5_15'):
            pd.DataFrame(trades).to_csv(f'trades46_{v}.csv', index=False, encoding='utf-8-sig')
    json.dump(results, open('results_step46.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('saved results_step46.json')


if __name__ == '__main__':
    main()
