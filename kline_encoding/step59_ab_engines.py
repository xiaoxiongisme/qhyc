# -*- coding: utf-8 -*-
"""step59 —— 两版「抓大波动」替代引擎，在 EG/EB（及 MA 对照）上回测。

目标：用户想交易 EG 9–10 月大波动，但 V5X 的 P2 高波日禁开仓恰好封锁这种行情。
      因此另起两套与 V5X 独立、且天然吃波动的打法，复用 step46 的同一成本/预热/EOD 口径。

Route A｜波动扩张突破 (vol-breakout)
  入场：当日 ATR / 近 20 根 ATR 均值 >= vol_ratio（波动扩张）
        AND 收盘突破前 N 根最高/最低（不含当根）且收在高位（c>o）
  方向：突破方向（上破多、下破空），不做 EMA140 方向过滤（那会与突破冲突）
  离场：2ATR 初始止损 + 2ATR 吊灯 + 0.5ATR 保本（与 V5X 同）

Route B｜偏离回归 (deviation-reversion / emadist)
  入场：|收盘 − EMA140| / ATR >= dev_thr（价格大幅偏离均值）
        偏离在上 → 做空回归；偏离在下 → 做多回归
  离场：价格回到 EMA140（均值回归止盈）+ 2ATR 硬止损 + 0.5ATR 保本

纪律：零前视（决策只用 <= 当根收盘）；指标增量/向量化；进场断言 ATR/EMA 有限；
      与 V5X 同成本模型（fee_row/slip/mult/tick）、同预热(30)、同 EOD 强平、同 OOS 切分(2026)。
"""
import json
import numpy as np
import pandas as pd
from step46_prod_bt import load_all, build_cost_fn, build_sym, metrics

SYMS = ['EG888', 'EB888', 'MA888']
ENTRY_MIN_I = 30
COOLDOWN = 3
BE_R = 0.5


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


# ---------------- 入场信号（向量化，严格零前视）----------------
def signal_A(h, l, c, o, BK_N, VOL_RATIO):
    """波动扩张突破：返回 sig 数组 (0/1/-1)。"""
    n = len(c)
    atr = atr14(h, l, c, 14)
    atr_ma = pd.Series(atr).rolling(20, min_periods=10).mean().to_numpy()
    vol_up = np.zeros(n, bool)
    m = (atr_ma > 0) & (~np.isnan(atr_ma)) & (~np.isnan(atr))
    vol_up[m] = (atr[m] / atr_ma[m]) >= VOL_RATIO
    hh = pd.Series(h).rolling(BK_N).max().shift(1).to_numpy()   # 前 N 根最高（不含当根）
    ll = pd.Series(l).rolling(BK_N).min().shift(1).to_numpy()
    sig = np.zeros(n)
    up = (c > hh) & (c > o) & vol_up
    dn = (c < ll) & (c < o) & vol_up
    sig[up] = 1
    sig[dn] = -1
    return sig, atr


# ---------------- 通用状态机 ----------------
def run_engine(sym, b, cost_tuple, tick, sig, atr, ema140, kind, params):
    o = b.open.values.astype(float); h = b.high.values.astype(float)
    l = b.low.values.astype(float); c = b.close.values.astype(float)
    raw_c = b.raw_c.values.astype(float)
    n = len(c)
    fee_row, slip, mult = cost_tuple
    allow = b.allow.values
    tds = b.td.values
    dtv = b.ts.values
    year = b.year.values

    SL_ATR = params['SL_ATR']
    TRAIL_ATR = params['TRAIL_ATR']
    # Route B 均值回归止盈目标用前一根 EMA140
    e140_prior = np.concatenate([[np.nan], ema140[:-1]])

    def fee(act, px_raw):
        fx, pc = fee_row[act]
        return fx + pc * px_raw * mult

    def cost_round(px_e_raw, px_x_raw, same_day):
        cx = fee('CLOSE_TODAY' if same_day else 'CLOSE_YEST', px_x_raw)
        return fee('OPEN', px_e_raw) + cx + 2 * slip * mult

    trades = []
    state = 0
    entry_px = avg_px = e_atr = 0.0
    lots = 1.0
    entry_i = -1
    cooldown = 0
    peak = trough = 0.0
    be_done = False
    entry_td = None
    entry_raw = 0.0

    for i in range(2, n):
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
            exit_now = False
            pnl_kind = ''
            if c[i] <= st:
                exit_now = True; pnl_kind = 'sl'
            elif kind == 'B' and not np.isnan(e140_prior[i]) and c[i] >= e140_prior[i]:
                exit_now = True; pnl_kind = 'tp'   # 回到均值
            if exit_now:
                fill = c[i]
                raw = (fill - avg_px) * lots * mult
                cost = cost_round(entry_raw, raw_c[i], tds[i] == entry_td) * lots
                net = raw - cost
                trades.append(dict(sym=sym, dir=1, edt=dtv[entry_i], xdt=dtv[i],
                                   P=avg_px, X=fill, lots=lots, e_atr=e_atr, net=net,
                                   gross=raw, r=net / (SL_ATR * e_atr * mult) if SL_ATR*e_atr*mult else 0,
                                   year=year[i], kind=kind, exit=pnl_kind))
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
            exit_now = False
            pnl_kind = ''
            if c[i] >= st:
                exit_now = True; pnl_kind = 'sl'
            elif kind == 'B' and not np.isnan(e140_prior[i]) and c[i] <= e140_prior[i]:
                exit_now = True; pnl_kind = 'tp'
            if exit_now:
                fill = c[i]
                raw = (avg_px - fill) * lots * mult
                cost = cost_round(entry_raw, raw_c[i], tds[i] == entry_td) * lots
                net = raw - cost
                trades.append(dict(sym=sym, dir=-1, edt=dtv[entry_i], xdt=dtv[i],
                                   P=avg_px, X=fill, lots=lots, e_atr=e_atr, net=net,
                                   gross=raw, r=net / (SL_ATR * e_atr * mult) if SL_ATR*e_atr*mult else 0,
                                   year=year[i], kind=kind, exit=pnl_kind))
                state = 0; cooldown = COOLDOWN; be_done = False; lots = 1.0

        # ---- 进场 ----
        if state == 0 and cooldown == 0 and i >= ENTRY_MIN_I and allow[i]:
            s = int(sig[i])
            if s != 0:
                a = atr[i]
                if np.isnan(a) or a <= 0:
                    pass
                else:
                    state = 1 if s > 0 else 2
                    entry_px = avg_px = c[i]
                    entry_raw = raw_c[i]
                    e_atr = a
                    entry_i = i
                    lots = 1.0
                    peak = trough = c[i]
                    be_done = False
                    entry_td = tds[i]
                    continue

    if state != 0:
        fill = c[-1]
        raw = ((fill - avg_px) if state == 1 else (avg_px - fill)) * lots * mult
        cost = cost_round(entry_raw, raw_c[-1], tds[-1] == entry_td) * lots
        net = raw - cost
        trades.append(dict(sym=sym, dir=state, edt=dtv[entry_i], xdt=dtv[-1],
                           P=avg_px, X=fill, lots=lots, e_atr=e_atr, net=net,
                           gross=raw, r=net / (SL_ATR * e_atr * mult) if SL_ATR*e_atr*mult else 0,
                           year=year[-1], kind=kind, exit='eod'))
    # 自检：任一笔 gross 为 NaN/inf 即报错
    for t in trades:
        assert np.isfinite(t['net']) and np.isfinite(t['r']), f'NaN trade {t}'
    return trades


def build_signals(b, kind, params):
    h = b.high.values.astype(float); l = b.low.values.astype(float)
    c = b.close.values.astype(float); o = b.open.values.astype(float)
    n = len(c)
    atr = atr14(h, l, c, 14)
    e140 = ema(c, 140)
    if kind == 'A':
        sig, atr_s = signal_A(h, l, c, o, params['BK_N'], params['VOL_RATIO'])
        return sig, atr_s, e140
    else:  # B
        e140_prior = np.concatenate([[np.nan], e140[:-1]])
        atr_prior = np.concatenate([[np.nan], atr[:-1]])
        dev = (c - e140_prior) / atr_prior
        sig = np.zeros(n)
        sig[dev >= params['DEV']] = -1
        sig[dev <= -params['DEV']] = 1
        # 过滤：EMA140 自身无效（前 140 根）不交易
        sig[np.isnan(dev)] = 0
        return sig, atr, e140


def main():
    ba, raw, dd, var, cost, fac = load_all()
    costmap = build_cost_fn(cost, var)
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    data = {}
    for s in SYMS:
        vc = s[:-3]
        if vc not in costmap:
            print(f'{s}: no cost, skip'); continue
        b = build_sym(s, ba, raw, dd, var)
        if b is None:
            continue
        # 清理前导 NaN 棒（EG/EB 序列起始有 6 根全 NaN；手写递推 ATR 遇 NaN 永久中毒）
        n0 = len(b)
        b = b.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(b) < 1500:
            continue
        if len(b) != n0:
            print(f'{s}: 清理 NaN 棒 {n0-len(b)} 根')
        data[s] = b
    print('loaded', list(data), flush=True)

    # 参数网格（小而诚实；单品种样本少，仅作概念验证，OOS 口径兜底）
    grids = {
        'A': [dict(BK_N=10, VOL_RATIO=1.3, SL_ATR=2.0, TRAIL_ATR=2.0),
              dict(BK_N=10, VOL_RATIO=1.6, SL_ATR=2.0, TRAIL_ATR=2.0),
              dict(BK_N=20, VOL_RATIO=1.3, SL_ATR=2.0, TRAIL_ATR=2.0),
              dict(BK_N=20, VOL_RATIO=1.6, SL_ATR=2.5, TRAIL_ATR=2.5),
              dict(BK_N=30, VOL_RATIO=1.5, SL_ATR=2.5, TRAIL_ATR=2.5)],
        'B': [dict(DEV=1.5, SL_ATR=2.0, TRAIL_ATR=2.0),
              dict(DEV=2.0, SL_ATR=2.0, TRAIL_ATR=2.0),
              dict(DEV=2.5, SL_ATR=2.5, TRAIL_ATR=2.5),
              dict(DEV=3.0, SL_ATR=2.5, TRAIL_ATR=2.5)],
    }

    results = {'A': {}, 'B': {}}
    for kind, grid in grids.items():
        for p in grid:
            tag = kind + ':' + json.dumps(p, sort_keys=True)
            allt = []
            per_sym = {}
            for s, b in data.items():
                vc = s[:-3]
                sig, atr, e140 = build_signals(b, kind, p)
                tr = run_engine(s, b, costmap[vc], tick.get(vc, 1.0), sig, atr, e140, kind, p)
                allt += tr
                per_sym[s] = metrics(tr, s)
            m = metrics(allt, tag)
            m['per_sym'] = per_sym
            results[kind][tag] = m
            print(f'{tag:55s} n={m.get("n")} win={m.get("win"):.2f} pf={m.get("pf")} '
                  f'net={m.get("net_yuan"):.0f} mdd={m.get("mdd_yuan"):.0f} mar={m.get("mar")} '
                  f'ho={m.get("holdout",{}).get("net_yuan")}', flush=True)

    json.dump(results, open('step59_results.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('saved step59_results.json')


if __name__ == '__main__':
    main()
