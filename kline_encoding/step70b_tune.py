# -*- coding: utf-8 -*-
"""Tune+validate rollover detection on 1m main-continuous.
Detector: 孤立大跳 (|ret|>RT 且相邻两根均 < RT2) + 跨年同日历窗复发 (同月且|日差|<=WIN 且跨>=2个不同年份)。
打印每品种每年命中数 + 样本，用于人工核对（MA 应在 3/9 月附近各一次/年；COVID 等一次性事件应被排除）。
"""
import pandas as pd, numpy as np, glob, os

RT = 0.012      # 候选跳阈值 1.2%
RT2 = 0.006     # 邻居冷静阈值 0.6%
WIN = 22        # 复发窗口(天)
MIN_YEARS = 2   # 至少跨几个不同年份

def load(sym):
    df = pd.read_csv(f'_sym1m/{sym}.csv')
    df['dt'] = pd.to_datetime(df['dt'], utc=True).dt.tz_convert(None)
    df = df.dropna(subset=['dt','c']).sort_values('dt').reset_index(drop=True)
    return df

def detect(df):
    c = df['c'].values.astype(float)
    n = len(c)
    ret = np.concatenate([[0.0], c[1:]/c[:-1]-1.0])
    # 邻居冷静
    calm_prev = np.concatenate([[True], np.abs(ret[1:-1]) < RT2, [True]])
    calm_next = np.concatenate([[True], np.abs(ret[2:]) < RT2, [True]])
    big = np.abs(ret) > RT
    cand = big & calm_prev & calm_next
    cand_idx = np.where(cand)[0]
    # 复发聚类
    kept = []
    dates = df['dt'].values
    for i in cand_idx:
        d = dates[i]
        # 同月、|日差|<=WIN、不同年份的候选
        cnt = 0
        yrs = set()
        for j in cand_idx:
            if j == i: 
                continue
            dd = dates[j]
            if dd.month == d.month and abs((dd - d).days) <= WIN:
                cnt += 1
                yrs.add(dd.year)
        if len(yrs) >= (MIN_YEARS-1):   # 加上自身年份共 >= MIN_YEARS
            kept.append(i)
    return ret, np.array(kept)

for sym in ['MA888','EG888','SC888','RB888','AU888','TA888']:
    df = load(sym)
    ret, kept = detect(df)
    dates = df['dt'].values
    print(f'\n=== {sym}  1m bars={len(df)}  candidates->kept rollovers={len(kept)} ===')
    # 每年每月分布
    yrs = {}
    for i in kept:
        d = dates[i]
        yrs.setdefault(d.year, []).append((d.month, d.day, round(ret[i],4)))
    for y in sorted(yrs):
        s = sorted(yrs[y])
        print(f'  {y}: ', s)
    # 看看被排除的大跳里有没有一次性事件（如 2020-02-03 COVID）
    # 打印所有候选里未被保留但 |ret|>3% 的
    c = df['c'].values.astype(float)
    big = np.abs(ret) > RT
    calm_prev = np.concatenate([[True], np.abs(ret[1:-1]) < RT2, [True]])
    calm_next = np.concatenate([[True], np.abs(ret[2:]) < RT2, [True]])
    cand = big & calm_prev & calm_next
    cand_idx = np.where(cand)[0]
    keptset = set(kept.tolist())
    big_ones = [(str(dates[i])[:10], round(ret[i],4)) for i in cand_idx if i not in keptset and abs(ret[i])>0.03]
    print(f'  被排除的 >3% 孤立跳 (一次性事件?) : {big_ones[:20]}')
