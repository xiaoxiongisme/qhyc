# -*- coding: utf-8 -*-
"""共享 loader 与工具（从 step24_dimensions.py 抽出，供深挖脚本复用）。
8 态八卦编码：实体方向/上影/下影各 2 位（00/01），open/chg/amp/clv 为派生列。
adj=True 表示与前一根间隔恰为 60 秒（会话内相邻）。
"""
import pandas as pd
import numpy as np

STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
SIDX = {s: i for i, s in enumerate(STATES)}
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
NAMES = [TRIG[s] for s in STATES]
ZONG = {2: 1, 1: 2, 5: 6, 6: 5}
CUO = {3: 4, 4: 3, 0: 7, 7: 0, 2: 5, 5: 2, 1: 6, 6: 1}


def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, '01', '00')
    up = np.where(h > np.maximum(o, c), '01', '00')
    lo = np.where(l < np.minimum(o, c), '01', '00')
    return np.char.add(np.char.add(body, up), lo)


def load(sym, parts):
    ds = []
    for f in parts:
        ds.append(pd.read_csv(f'{sym}_{f}_1m_raw.csv', parse_dates=['datetime']))
    d = pd.concat(ds, ignore_index=True).sort_values('datetime').reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    rng = h - l
    safe = np.where(rng > 0, rng, 1.0)
    d['state'] = np.array([SIDX[x] for x in encode_ohlc(o, h, l, c)])
    d['chg'] = (c - o) / o * 100.0
    d['amp'] = rng / o * 100.0
    d['clv'] = np.where(rng > 0, (c - l) / safe, 0.5)
    d['logret'] = np.log(c / np.where(o > 0, o, 1.0))   # 单根对数收益（会话内）
    gap = d.datetime.diff().dt.total_seconds().values
    d['adj'] = np.concatenate([[False], gap[1:] == 60.0])
    d['year'] = d.datetime.dt.year.values
    d['td'] = (d.datetime + pd.to_timedelta((d.datetime.dt.hour >= 21).astype(int), unit='D')).dt.date.values
    d['dow'] = pd.to_datetime(pd.Series(d.td)).dt.dayofweek.values
    d['month'] = d.datetime.dt.month.values
    seg = np.concatenate([[0], np.cumsum(gap[1:] != 60.0)])
    d['seg'] = seg
    d['seq'] = d.groupby('seg').cumcount().values
    seg_hour = d.groupby('seg').datetime.first().dt.hour
    BASE = {9: 0, 10: 75, 13: 135, 21: 225}
    d['slot'] = d.seq + d.seg.map(seg_hour).map(BASE).values
    # 成交量自检
    if 'volume' in d.columns:
        d['volume'] = pd.to_numeric(d['volume'], errors='coerce').fillna(0.0)
    return d


def qbin(x, k):
    qs = np.quantile(x, [(i + 1) / k for i in range(k - 1)])
    return np.clip(np.digitize(x, qs), 0, k - 1)


def js_div(p, q):
    m = 0.5 * (p + q)
    def kl(a, b):
        nz = a > 0
        return float((a[nz] * np.log2(a[nz] / b[nz])).sum())
    return 0.5 * kl(p, m) + 0.5 * kl(q, m)


def adj_pairs(d):
    """会话内相邻对 (state_t, state_{t+1}, idx_{t+1})"""
    m = d.adj.values[1:]
    return d.state.values[:-1][m], d.state.values[1:][m], np.where(m)[0] + 1


def spearman(a, b):
    from scipy.stats import spearmanr
    return float(spearmanr(a, b).statistic)


def save_json(obj, path):
    import json
    def cv(x):
        if isinstance(x, (np.integer,)): return int(x)
        if isinstance(x, (np.floating,)): return float(x)
        if isinstance(x, np.ndarray): return x.tolist()
        return str(x)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=cv)
