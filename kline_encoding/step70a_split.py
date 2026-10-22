# -*- coding: utf-8 -*-
"""PASS 1: 把统一主连 1m (kqm_1m.tsv.gz, 2016-2026) 按品种拆成 _sym1m/{sym}.csv。
只保留 prod50 的 59 个品种；dt 先以字符串保留，step70b 再解析。
列: dt,o,h,l,c,v,oi  (float32 省空间)
"""
import pandas as pd, os, time

SRC = 'D:/学习资料/_etl_out/kqm_1m.tsv.gz'
OUT = '_sym1m'
os.makedirs(OUT, exist_ok=True)

ba = pd.read_csv('prod50/bar_60m_ba.csv.gz', usecols=['symbol'])
syms = sorted(set(ba.symbol.unique()))
handles = {s: open(f'{OUT}/{s}.csv', 'w') for s in syms}
for s, h in handles.items():
    h.write('dt,o,h,l,c,v,oi\n')

t0 = time.time(); n = 0
for ch in pd.read_csv(SRC, sep='\t', header=None,
                      usecols=[0, 1, 2, 3, 4, 5, 6, 8],
                      names=['sym', 'dt', 'o', 'h', 'l', 'c', 'v', 'oi'],
                      dtype={'sym': 'string', 'o': 'float32', 'h': 'float32',
                             'l': 'float32', 'c': 'float32', 'v': 'float32', 'oi': 'float32'},
                      chunksize=2_000_000, na_values=['\\N'], on_bad_lines='skip'):
    sub = ch[ch['sym'].isin(syms)]
    for s, g in sub.groupby('sym', sort=False):
        g[['dt', 'o', 'h', 'l', 'c', 'v', 'oi']].to_csv(handles[s], header=False, index=False)
        handles[s].flush()
    n += len(ch)
    print(f'{n:,} rows  {round(time.time()-t0, 0):.0f}s', flush=True)

for h in handles.values():
    h.close()
print('DONE total_rows=', f'{n:,}')
