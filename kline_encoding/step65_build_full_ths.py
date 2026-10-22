# -*- coding: utf-8 -*-
"""用 1m 主连重建【全部品种】的同花顺连续60m网格(全历史)，并套用 bar_60m_ba 刻度。

相对 rebuild_60m_ths.py 的唯一修正：
  night 判定由「hour in {21,22,23}」改为【段首小时】判定
  (seg_first_hour>=18 或 <=3)，从而正确处理金属/原油跨午夜夜盘
  (21:00->02:30 整段归夜盘，含 00:00-02:30 的尾段，不再误判为日盘)。
  其余(CHUNK=60 连续累加、按会话中位数复权比)与原方法一致。

输出：
  bar_60m_ths_full_rebuilt.csv.gz  —— 全样本(全部有1m的品种) 同花顺网格
  (验证后由 step66 复制到 prod50/bar_60m_ths.csv.gz)
"""
import os, sys
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
GAP_MIN = 200
CHUNK = 60
EXCLUDE = {'CJ'}          # 红枣 ba 损坏，不参与 THS 重建
OUT = os.path.join(BASE, 'bar_60m_ths_full_rebuilt.csv.gz')


def load_1m(var):
    files = [f'{var}_2020_1m_raw.csv', f'{var}_2021_2025_1m_raw.csv', f'{var}_2026_1m_raw.csv']
    parts = []
    for f in files:
        p = os.path.join(BASE, f)
        if not os.path.exists(p) or os.path.getsize(p) < 1024:
            continue
        d = pd.read_csv(p, usecols=['datetime', 'open', 'high', 'low', 'close', 'volume', 'open_interest', 'close_interest'])
        parts.append(d)
    if not parts:
        return None
    d = pd.concat(parts, ignore_index=True)
    d['datetime'] = pd.to_datetime(d['datetime'])
    d = d.dropna(subset=['datetime', 'open', 'high', 'low', 'close'])
    d = d.sort_values('datetime').drop_duplicates('datetime').reset_index(drop=True)
    return d


def build_ba_lookup(ba, sym):
    b = ba[ba.symbol == sym][['ts', 'close']]
    return {(sym, r.ts.strftime('%Y-%m-%d %H:%M')): r.close for _, r in b.iterrows()}


def rebuild_symbol(var, d1m, ba_lookup):
    sym = var + '888'
    dt = d1m['datetime'].values
    n = len(d1m)
    o = d1m['open'].values; h = d1m['high'].values; l = d1m['low'].values
    c = d1m['close'].values; v = d1m['volume'].values
    oi = d1m['close_interest'].values if 'close_interest' in d1m else d1m['open_interest'].values
    hours = d1m['datetime'].dt.hour.values
    # 段切分（会话边界：间隔 > GAP_MIN 分钟）
    gaps = np.diff(dt).astype('timedelta64[m]').astype(float)
    seg_id = np.concatenate([[0], np.cumsum(gaps > GAP_MIN)])
    # 段首行索引
    change = np.empty(n, dtype=bool); change[0] = True; change[1:] = seg_id[1:] != seg_id[:-1]
    starts = np.where(change)[0]
    seg_start_idx = starts[np.searchsorted(starts, np.arange(n), side='right') - 1]
    pos_in_seg = np.arange(n) - seg_start_idx
    bar_in_seg = pos_in_seg // CHUNK
    # 段首小时（用于 night 判定：跨午夜夜盘整段归夜）
    sfh_per_row = hours[seg_start_idx]
    bar_id = seg_id.astype(np.int64) * 2_000_000 + bar_in_seg.astype(np.int64)
    df = pd.DataFrame({'bar_id': bar_id, 'ts': dt, 'open': o, 'high': h, 'low': l,
                       'close': c, 'vol': v, 'oi': oi, 'sfh': sfh_per_row})
    g = df.groupby('bar_id', sort=True).agg(
        ts=('ts', 'first'), open=('open', 'first'), high=('high', 'max'),
        low=('low', 'min'), close=('close', 'last'), volume=('vol', 'sum'),
        oi=('oi', 'last'), sfh=('sfh', 'first')).reset_index(drop=True)
    df = g
    df['symbol'] = sym
    # 复权比：ba_close[该标签] / 重建未复权 close
    df['ba_c'] = df['ts'].dt.strftime('%Y-%m-%d %H:%M').map(lambda k: ba_lookup.get((sym, k), np.nan))
    df['ratio_cand'] = df['ba_c'] / df['close']
    df['date'] = df.ts.dt.normalize()
    # night 用【段首小时】判定（修正金属/原油跨午夜）
    is_night = ((df['sfh'].values >= 18) | (df['sfh'].values <= 3))
    df['is_night'] = is_night
    day = df[~df.is_night]; night = df[df.is_night]
    day_ratio = day.groupby('date')['ratio_cand'].median()
    night_ratio = night.groupby('date')['ratio_cand'].median()
    dr = df['date'].map(day_ratio).ffill().bfill()
    nr = df['date'].map(night_ratio).ffill().bfill()
    rr = dr.where(~df.is_night, nr).fillna(dr).fillna(1.0)
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col] * rr.values
    df = df.drop(columns=['ba_c', 'ratio_cand', 'date', 'is_night', 'oi', 'sfh'])
    return df


def main():
    ba = pd.read_csv(os.path.join(BASE, 'prod50/bar_60m_ba.csv.gz'))
    ba['ts'] = pd.to_datetime(ba['ts'])
    # 有本地 1m 的全部品种
    allvars = set()
    for f in os.listdir(BASE):
        if f.endswith('_1m_raw.csv') and '_' in f[:-len('_1m_raw.csv')]:
            v = f[:-len('_1m_raw.csv')].split('_')[0]
            allvars.add(v)
    # 仅保留 bar_60m_ba 中的品种
    ba_vars = set(s[:-3] for s in ba.symbol.unique())
    vars_todo = sorted(v for v in allvars if v in ba_vars and v not in EXCLUDE)
    print(f'将重建 {len(vars_todo)} 个品种 (有1m & 在ba中 & 非CJ)')
    outs = []
    for var in vars_todo:
        d1m = load_1m(var)
        if d1m is None or len(d1m) == 0:
            print(f'{var}: 无1m, 跳过'); continue
        ba_lookup = build_ba_lookup(ba, var + '888')
        df = rebuild_symbol(var, d1m, ba_lookup)
        if df is None or len(df) == 0:
            print(f'{var}: 重建为空, 跳过'); continue
        hm = sorted(df.ts.dt.strftime('%H:%M').unique())
        cnt = df.groupby(df.ts.dt.date).size()
        outs.append(df)
        print(f'{var}: 1m={len(d1m):,} -> 60m={len(df):,}  {df.ts.min().date()}~{df.ts.max().date()} '
              f'每日根数mode={int(cnt.mode().iloc[0])} starts={hm}')
    res = pd.concat(outs, ignore_index=True)
    res = res.sort_values(['symbol', 'ts']).reset_index(drop=True)
    res['grid_verified'] = True
    res['hold'] = np.nan
    cols = ['symbol', 'ts', 'open', 'high', 'low', 'close', 'volume', 'hold', 'grid_verified']
    res = res.reindex(columns=cols)
    res.to_csv(OUT, index=False, compression='gzip')
    print(f'\nSAVED {OUT}  rows={len(res):,} symbols={res.symbol.nunique()}')


if __name__ == '__main__':
    main()
