# -*- coding: utf-8 -*-
"""
把 1 分钟数据重建成「同花顺口径」的 60m K 线，并套用与云端 bar_60m_ba 一致的复权比。

同花顺 60m 口径（经实机验证 + 1m 会话结构推导）：
  - 不是整点桶，而是「连续 60 个交易分钟」累加为一根（跨 10:15-10:30 小息、跨午餐都不切）
  - 化工/黑色(09:00-10:15,10:30-11:30,13:30-15:00,21:00-23:00) => 每日 6 根：
        09:00 / 10:00 / 11:15 / 14:15  (日盘)  +  21:00 / 22:00 (夜盘)
  - 日盘与夜盘是两段连续累加（15:00->21:00、23:00->次日09:00 之间断开）

复权比修正（2026-10-21）：
  原实现用「单日 median(ba/raw)」做日内恒定比，但在合约换月发生在日盘↔夜盘之间时，
  单日中位数会偏向某一侧，导致另一侧（多为夜盘）重建价与 MA888 偏差最大 26%。
  改为【分段复权比】：日盘比 = median(ba/raw over 日盘整数网格标签 09/10/11/13/14/15)，
  夜盘比 = median(ba/raw over 夜盘标签 21/22/23)，按 bar 所属会话分别套用。

SC 剔除：SC 跨午夜、Sina 60m 标签与同花顺 SC 网格对不上，无法干净映射，从交付物排除。
"""
import sys, glob, os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
GAP_MIN = 200          # 段切分阈值（分钟）
CHUNK = 60             # 每根 N 个 1m
COLS_OUT = ['symbol', 'ts', 'open', 'high', 'low', 'close', 'volume', 'open_interest']


def load_1m(var):
    files = [f'{var}_2020_1m_raw.csv', f'{var}_2021_2025_1m_raw.csv', f'{var}_2026_1m_raw.csv']
    parts = []
    for f in files:
        p = os.path.join(BASE, f)
        if not os.path.exists(p) or os.path.getsize(p) < 1024:
            continue
        d = pd.read_csv(p, usecols=['datetime', 'open', 'high', 'low', 'close', 'volume',
                                    'open_interest', 'close_interest'])
        parts.append(d)
    if not parts:
        return None
    d = pd.concat(parts, ignore_index=True)
    d['datetime'] = pd.to_datetime(d['datetime'])
    d = d.dropna(subset=['datetime', 'open', 'high', 'low', 'close'])
    d = d.sort_values('datetime').drop_duplicates('datetime').reset_index(drop=True)
    return d


def build_ba_lookup(ba, sym):
    """(symbol, 'YYYY-MM-DD HH:MM') -> ba.close  仅取该品种在云端后复权整数网格上的收盘价。"""
    b = ba[ba.symbol == sym][['ts', 'close']]
    return {(sym, r.ts.strftime('%Y-%m-%d %H:%M')): r.close for _, r in b.iterrows()}


SESSION_NIGHT_HOURS = {21, 22, 23}


def rebuild_symbol(var, d1m, ba_lookup):
    """从 1m 原始(未复权前月)重建同花顺 60m，再按【1m重建价 vs 云端 ba】逐日/逐会话复权比对齐到 MA888 刻度。

    关键修正：复权比分母必须是「重建所用的 1m 原始」，而非云端另一套连续序列(bar_60m_raw)。
    换月日 1m 原始与前/后合约不同步时，用 1m 作分母才能把重建价正确抬到 ba 刻度
    （旧实现用 bar_60m_raw 作分母→比≈1→换月夜盘差 20%+）。
    比按会话(日盘/夜盘)分开取中值，处理日↔夜之间的换月。
    """
    sym = var + '888'
    dt = d1m['datetime'].values
    # 段切分
    gaps = np.diff(dt).astype('timedelta64[m]').astype(float)
    seg_id = np.concatenate([[0], np.cumsum(gaps > GAP_MIN)])
    bars = []
    o = d1m['open'].values; h = d1m['high'].values; l = d1m['low'].values
    c = d1m['close'].values; v = d1m['volume'].values
    oi = d1m['close_interest'].values if 'close_interest' in d1m else d1m['open_interest'].values
    idx0 = 0
    n = len(d1m)
    while idx0 < n:
        seg = seg_id[idx0]
        end = idx0
        while end + 1 < n and seg_id[end + 1] == seg:
            end += 1
        for s in range(idx0, end + 1, CHUNK):
            e = min(s + CHUNK - 1, end)
            bars.append((sym, dt[s], o[s], np.nanmax(h[s:e + 1]), np.nanmin(l[s:e + 1]),
                         c[e], np.nansum(v[s:e + 1]), oi[e]))
        idx0 = end + 1
    df = pd.DataFrame(bars, columns=COLS_OUT)
    df['ts'] = pd.to_datetime(df['ts'])
    # 逐 bar 复权候选比 = ba_close[该标签] / 重建未复权 close（两者均为该 60m 窗终点收价）
    df['ba_c'] = df['ts'].dt.strftime('%Y-%m-%d %H:%M').map(lambda k: ba_lookup.get((sym, k), np.nan))
    df['ratio_cand'] = df['ba_c'] / df['close']
    df['date'] = df.ts.dt.normalize()
    df['is_night'] = df.ts.dt.hour.isin(SESSION_NIGHT_HOURS)
    day = df[~df.is_night]; night = df[df.is_night]
    day_ratio = day.groupby('date')['ratio_cand'].median()
    night_ratio = night.groupby('date')['ratio_cand'].median()
    dr = df['date'].map(day_ratio).ffill().bfill()
    nr = df['date'].map(night_ratio).ffill().bfill()
    rr = dr.where(~df.is_night, nr).fillna(dr).fillna(1.0)
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col] * rr.values
    df = df.drop(columns=['ba_c', 'ratio_cand', 'date', 'is_night'])
    return df


def main():
    syms = sys.argv[1:] or ['MA', 'EG', 'EB', 'SA', 'FG', 'L', 'PF', 'PP', 'PX', 'TA', 'V', 'UR', 'IF', 'PR']
    ba = pd.read_csv(os.path.join(BASE, 'prod50/bar_60m_ba.csv.gz'))
    ba['ts'] = pd.to_datetime(ba['ts'])
    outs = []
    for var in syms:
        if var == 'SC':
            print('SC: 跨午夜且 Sina 标签无法映射同花顺网格，从交付物剔除'); continue
        d1m = load_1m(var)
        if d1m is None or len(d1m) == 0:
            print(f'{var}: 无 1m 数据，跳过'); continue
        ba_lookup = build_ba_lookup(ba, var + '888')
        df = rebuild_symbol(var, d1m, ba_lookup)
        hm = sorted(df.ts.dt.strftime('%H:%M').unique())
        cnt = df.groupby(df.ts.dt.date).size()
        print(f'{var}: 1m={len(d1m)} -> 60m={len(df)} 覆盖 {df.ts.min()}~{df.ts.max()}')
        print(f'    HH:MM={hm}')
        print(f'    每日根数: mode={int(cnt.mode().iloc[0])} dist={dict(cnt.value_counts().head(4))}')
        outs.append(df)
    if outs:
        res = pd.concat(outs, ignore_index=True)
        outp = os.path.join(BASE, 'bar_60m_ths.csv.gz')
        res.to_csv(outp, index=False)
        print(f'\nSAVED {outp}  rows={len(res)} symbols={res.symbol.nunique()}')


if __name__ == '__main__':
    main()
