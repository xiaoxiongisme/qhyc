#!/usr/bin/env python
# 删除 CJ888（红枣主连）全部数据行 —— 仅修数据、不改表结构。
# 执行前先对受影响表做精确字节备份（.bak_cj），删除后做自检。
import shutil, os, gzip, pandas as pd

PROD = 'prod50'
TARGET = 'CJ888'
TABLES = ['bar_60m_ba', 'bar_60m_raw', 'daily_bar']  # THS 无 CJ，跳过

print('=== 1) 备份（字节级 copy）===')
for t in TABLES:
    src = f'{PROD}/{t}.csv.gz'
    if not os.path.exists(src):
        print(f'  {t}: 源文件缺失，跳过'); continue
    dst = f'{PROD}/{t}.csv.gz.bak_cj'
    shutil.copy(src, dst)
    # 校验备份可读（文件名非 .gz 结尾，须显式指定压缩）
    _ = pd.read_csv(dst, nrows=1, compression='gzip')
    print(f'  {t}: 已备份 -> {dst}  ({os.path.getsize(dst)//1024} KB)')

print('\n=== 2) 删除 CJ888 并自检 ===')
for t in TABLES:
    src = f'{PROD}/{t}.csv.gz'
    if not os.path.exists(src):
        print(f'  {t}: 缺失，跳过'); continue
    df = pd.read_csv(src)
    total0 = len(df)
    cj_mask = df['symbol'] == TARGET
    n_cj = int(cj_mask.sum())
    df2 = df[~cj_mask].copy()
    total1 = len(df2)
    # 写回（保持原 gzip 格式与列序）
    df2.to_csv(src, index=False, compression='gzip')
    # 自检
    chk = pd.read_csv(src)
    remain_cj = int((chk['symbol'] == TARGET).sum())
    assert remain_cj == 0, f'{t}: 删除后仍有 CJ888！'
    assert len(chk) == total0 - n_cj, f'{t}: 行数不符 {len(chk)} != {total0-n_cj}'
    print(f'  {t}: {total0} -> {total1}  (删 CJ888={n_cj}, 残留 CJ888={remain_cj}, 校验 OK)')

print('\n=== 3) 全表 CJ 残留复核 ===')
for t in ['bar_60m_ba', 'bar_60m_raw', 'bar_60m_ths', 'daily_bar']:
    p = f'{PROD}/{t}.csv.gz'
    if not os.path.exists(p):
        print(f'  {t}: 缺失'); continue
    d = pd.read_csv(p, usecols=['symbol'])
    cj = sorted([s for s in d['symbol'].unique() if str(s).upper().startswith('CJ')])
    print(f'  {t}: CJ 系列符号 = {cj if cj else "(无)"}')

print('\n完成。')
