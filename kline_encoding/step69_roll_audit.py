# -*- coding: utf-8 -*-
"""换月 bug 跨表审计（数据级，不改表结构）。

逻辑：后复权序列 ba 在换月处应"连续"（换月跳空被复权吸收进比值 r=ba/raw 的台阶）；
若 ba 在换月处与 raw 一起跳（r 不台阶），则是"未复权/换月错配"——即此前 THS 重建里的同类 bug。

判定：
  r = ba.close / raw.close
  1) 全局：r 是否偏离 1（后复权是否存在）。若 r≈1 全程 → ba 根本没复权（最严重 bug）。
  2) 逐换月候选：raw 在"会话/日界"大跳(>2%) 且 上一根间隔>60min → 换月候选；
     该根若 |ba_ret| > 0.5*|raw_ret|（ba 跟着 raw 一起跳）→ 该换月未正确复权（flag）。
     若 ba 连续（|ba_ret| 小）→ 正确。
输出：每品种换月候选数 / flag 数 / r 范围；并对 flag 最多的品种打印细节。
"""
import pandas as pd, numpy as np, json, os

DATA = 'prod50'
ba = pd.read_csv(f'{DATA}/bar_60m_ba.csv.gz', parse_dates=['ts']).sort_values(['symbol','ts'])
raw = pd.read_csv(f'{DATA}/bar_60m_raw.csv.gz', parse_dates=['ts']).sort_values(['symbol','ts'])

syms = sorted(set(ba.symbol) & set(raw.symbol))
report = {}
details = []
for s in syms:
    b = ba[ba.symbol==s].reset_index(drop=True)
    r = raw[raw.symbol==s].reset_index(drop=True)
    # 对齐到共同 ts
    m = b.merge(r[['ts','close']], on='ts', suffixes=('','_raw'), how='inner').sort_values('ts')
    if len(m) < 50:
        continue
    bc = m['close'].values.astype(float)
    rc = m['close_raw'].values.astype(float)
    r_ratio = bc / rc
    # 全局 r 统计
    rmin, rmax = np.nanmin(r_ratio), np.nanmax(r_ratio)
    # 换月候选
    ts = m['ts'].values
    gaps = np.diff(ts).astype('timedelta64[m]').astype(float)
    bc_ret = np.diff(bc) / bc[:-1]
    rc_ret = np.diff(rc) / rc[:-1]
    cand = (np.abs(rc_ret) > 0.02) & (gaps > 60)
    cand_idx = np.where(cand)[0] + 1  # 对应第 i 根
    n_cand = len(cand_idx)
    flag_idx = [i for i in cand_idx if np.abs(bc_ret[i-1]) > 0.5*np.abs(rc_ret[i-1])]
    n_flag = len(flag_idx)
    report[s] = dict(n_cand=n_cand, n_flag=n_flag, rmin=round(float(rmin),4), rmax=round(float(rmax),4))
    if n_flag > 0:
        # 记录前 3 个 flag 的细节
        for i in flag_idx[:3]:
            details.append(dict(sym=s, ts=str(ts[i]), raw_ret=round(float(rc_ret[i-1]),4),
                                ba_ret=round(float(bc_ret[i-1]),4), r=round(float(r_ratio[i]),4)))

# 汇总
df = pd.DataFrame(report).T
df['flag_rate'] = (df.n_flag / df.n_cand.replace(0,np.nan)).fillna(0)
print('=== 换月 bug 审计：bar_60m_ba vs bar_60m_raw ===')
print(f'审计品种数: {len(df)}')
print(f'有 flag（疑似未复权换月）的品种数: {(df.n_flag>0).sum()}')
print('\n按 flag 数排序（前 15）:')
print(df.sort_values('n_flag', ascending=False).head(15).to_string())
print('\nr 完全≈1（ba 根本未复权）的品种（rmax-rmin<0.005）:')
flat = df[(df.rmax - df.rmin) < 0.005]
print(f'  数量: {len(flat)} -> {list(flat.index)}')

# 详细
if details:
    print('\n=== flag 细节样本（前 20）===')
    for d in details[:20]:
        print(d)

json.dump({'report': report, 'flat_unadjusted': list(flat.index),
           'n_flag_syms': int((df.n_flag>0).sum())},
          open('roll_audit_ba.json','w'), ensure_ascii=False, indent=1)
print('\n已写出 roll_audit_ba.json')
