# -*- coding: utf-8 -*-
"""修复唯一确认的真实数据缺陷：bar_60m_ba / bar_60m_raw 中【前导全 NaN OHLC 棒】(25 品种, 203 根, 均在序列前5~12根)。
只删无效行(修数据值)，不改字段/结构。先备份。
结论(见 verify_clean_report.json)：换月污染 bug 不存在 —— ba 已正确后复权(零年度复发孤立跳; r=ba/raw 在换月处阶梯向下=正确吸收)；raw 为未调整参考(设计如此)；ths 无 NaN。
"""
import pandas as pd, numpy as np, shutil, os, gzip

for f in ['bar_60m_ba.csv.gz', 'bar_60m_raw.csv.gz']:
    src = f'prod50/{f}'
    bak = f'prod50/{f}.bak_nan'
    if not os.path.exists(bak):
        shutil.copy(src, bak)
        print('backup ->', bak)
    df = pd.read_csv(src, parse_dates=['ts'])
    ohld = [c for c in ['open','high','low','close'] if c in df.columns]
    n0 = len(df)
    bad = df[df[ohld].isna().any(axis=1)]
    # 仅删前导 NaN（序列开始 15 根内），避免误删中部 sporadic 行
    keep_mask = np.ones(len(df), dtype=bool)
    for s, g in bad.groupby('symbol'):
        gi = g.sort_values('ts')
        start = df[df.symbol==s].index.min()
        pos = gi.index - start
        lead = gi.index[pos < 15]
        keep_mask[lead] = False
    df2 = df[keep_mask].reset_index(drop=True)
    df2.to_csv(src, index=False, compression='gzip')
    print(f'{f}: {n0} -> {len(df2)}  (删 {n0-len(df2)} 前导NaN棒)')
    # 复核
    dfc = pd.read_csv(src, parse_dates=['ts'])
    rem = dfc[dfc[ohld].isna().any(axis=1)]
    print(f'  复核剩余 NaN-OHLC 棒: {len(rem)}')
