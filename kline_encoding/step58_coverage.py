# -*- coding: utf-8 -*-
"""检查 EB/EG 数据实际覆盖到的日期，特别是是否包含 2026 年 9-10 月（用户所说大波动期）。"""
import numpy as np
import pandas as pd
import step46_prod_bt as s46

ba, raw, dd, var, cost, fac = s46.load_all()
for s in ['EB888', 'EG888']:
    b = s46.build_sym(s, ba, raw, dd, var)
    ts = pd.to_datetime(b.ts)
    print(f'\n=== {s} ===')
    print(f'  首根: {ts.min()}   末根: {ts.max()}   共 {len(b)} 根')
    ym = ts.dt.to_period('M')
    cnt = ym.value_counts().sort_index()
    # 2026 各月
    print('  2026 各月 60m 根数:')
    for p, c in cnt.items():
        if str(p).startswith('2026'):
            print(f'    {p}: {c}')
    # hvol 天占比（2026）
    dd2026 = b[ts.dt.year == 2026]
    hv = dd2026.hvol.mean() if len(dd2026) else float('nan')
    print(f'  2026 高波日(hvol)占比: {hv*100:.1f}%   (交易日数≈{dd2026.td.nunique()})')
    # 2026 日振幅均值（用 daily_bar? 这里用 60m 当根高低）
    rng = (dd2026.high - dd2026.low).mean()
    print(f'  2026 单根 60m 平均振幅(H-L): {rng:.1f}')
