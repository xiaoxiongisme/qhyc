# -*- coding: utf-8 -*-
"""Step 33: 从本地交易所 zip 提取板块品种主力连续 1m 数据
输出与既有文件同构：{SYM}_2020_1m_raw.csv / {SYM}_2021_2025_1m_raw.csv / {SYM}_2026_1m_raw.csv
ke_common.load(sym, PARTS) 直接可用。MA/EG 已有，不重提。
"""
import zipfile, io, os
import pandas as pd

OUT = 'E:/抖音分析/2026-10-16-19-50-44/kline_encoding'
Z = {'CZCE': 'D:/学习资料/CZCE_郑州商品交易所.zip',
     'DCE': 'D:/学习资料/DCE_大连商品交易所.zip',
     'CFFEX': 'D:/学习资料/CFFEX_中国金融期货交易所.zip',
     'INE': 'D:/学习资料/INE_上海国际能源交易中心.zip'}

SPEC = [  # (sym, zip, member)；member=None 表示按年分文件
    ('TA', 'CZCE', 'TA_PTA/00_主力连续/TA_main_1m.csv'),
    ('PF', 'CZCE', 'PF_短纤/00_主力连续/PF_main_1m.csv'),
    ('PX', 'CZCE', 'PX_对二甲苯/00_主力连续/PX_main_1m.csv'),
    ('PR', 'CZCE', 'PR_瓶片/00_主力连续/PR_main_1m.csv'),
    ('SA', 'CZCE', 'SA_纯碱/00_主力连续/SA_main_1m.csv'),
    ('FG', 'CZCE', 'FG_玻璃/00_主力连续/FG_main_1m.csv'),
    ('UR', 'CZCE', 'UR_尿素/00_主力连续/UR_main_1m.csv'),
    ('PP', 'DCE', 'PP_聚丙烯/00_主力连续/PP_main_1m.csv'),
    ('L', 'DCE', 'L_塑料/00_主力连续/L_main_1m.csv'),
    ('V', 'DCE', 'V_PVC/00_主力连续/V_main_1m.csv'),
    ('EB', 'DCE', 'EB_苯乙烯/00_主力连续/EB_main_1m.csv'),
    ('IF', 'CFFEX', 'IF_沪深300股指/00_主力连续/IF_main_1m.csv'),
    ('SC', 'INE', None),   # SC_main_1m_2018..2026.csv 按年分文件
]
PARTS = ('2020', '2021_2025', '2026')


def year_of(p):
    return 2020 if p == '2020' else (2026 if p == '2026' else None)


def extract(sym, zp, member):
    paths = {p: f'{OUT}/{sym}_{p}_1m_raw.csv' for p in PARTS}
    fhs = {p: open(v, 'w', encoding='utf-8', newline='') for p, v in paths.items()}
    counts = {p: 0 for p in PARTS}
    rng = {p: [None, None] for p in PARTS}
    members = [member] if member else [f'SC_原油/00_主力连续/SC_main_1m_{y}.csv' for y in range(2020, 2027)]
    with zipfile.ZipFile(zp) as z:
        names = set(z.namelist())
        first = True
        for mem in members:
            if mem not in names:
                print(f'  [warn] {sym}: member 不存在 {mem}')
                continue
            with z.open(mem) as f:
                wrap = io.TextIOWrapper(f, encoding='utf-8-sig')
                header = wrap.readline().strip()
                if first:
                    for p in PARTS:
                        fhs[p].write(header + '\n')
                    first = False
                for ch in pd.read_csv(wrap, names=header.split(','), chunksize=400_000):
                    yr = pd.to_datetime(ch.datetime).dt.year
                    for p in PARTS:
                        y = year_of(p)
                        m = (yr == y) if y else ((yr >= 2021) & (yr <= 2025))
                        if m.any():
                            sub = ch[m]
                            sub.to_csv(fhs[p], header=False, index=False)
                            counts[p] += int(m.sum())
                            d0, d1 = sub.datetime.min(), sub.datetime.max()
                            if rng[p][0] is None or d0 < rng[p][0]:
                                rng[p][0] = d0
                            if rng[p][1] is None or d1 > rng[p][1]:
                                rng[p][1] = d1
    for p in PARTS:
        fhs[p].close()
    return counts, rng


for sym, zk, member in SPEC:
    counts, rng = extract(sym, Z[zk], member)
    tot = sum(counts.values())
    print(f'{sym}: {tot:,} bars | ' + ' | '.join(f'{p} {counts[p]:,}' for p in PARTS))
    for p in PARTS:
        if counts[p]:
            print(f'    {p}: {rng[p][0]} ~ {rng[p][1]}')
print('ALL DONE')
