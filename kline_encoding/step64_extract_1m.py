# -*- coding: utf-8 -*-
"""从交易所 ZIP 提取全历史 1m 主连数据，补齐 43 个非金标准品种（金标准14已本地存在）。
产出与 rebuild_60m_ths.load_1m 完全兼容的 {var}_2020_1m_raw.csv / {var}_2021_2025_1m_raw.csv / {var}_2026_1m_raw.csv。

兼容两种 ZIP 内部结构：
  - CZCE/DCE/GFEX/INE/CFFEX: 单文件 `VAR_xxx/00_主力连续/VAR_main_1m.csv` (含全历史)
  - SHFE: 按年拆分 `VAR_xxx/00_主力连续/VAR_main_1m_YYYY.csv`
两种都用「逐行年份路由」统一处理。
"""
import os, json, zipfile
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
ZIPS = {
    'CZCE': 'D:/学习资料/CZCE_郑州商品交易所.zip',
    'DCE':  'D:/学习资料/DCE_大连商品交易所.zip',
    'SHFE': 'D:/学习资料/SHFE_上海期货交易所.zip',
    'INE':  'D:/学习资料/INE_上海国际能源交易中心.zip',
    'CFFEX':'D:/学习资料/CFFEX_中国金融期货交易所.zip',
    'GFEX': 'D:/学习资料/GFEX_广州期货交易所.zip',
}

# bar_60m_ba 全部品种
ba = pd.read_csv(os.path.join(BASE, 'prod50/bar_60m_ba.csv.gz'))
ba_syms = sorted(set(s[:-3] for s in ba.symbol.unique()))

# 金标准14 已有本地 1m (2021_2025 文件存在即视为已覆盖)
GOLD = {'EB','EG','FG','IF','L','MA','PF','PP','PR','PX','SA','TA','UR','V'}
def have_local(v):
    return os.path.exists(os.path.join(BASE, f'{v}_2021_2025_1m_raw.csv'))
NEED = [v for v in ba_syms if v not in GOLD]   # 含 SC 等; 全部补齐（SC 后续按对齐质量决定）
print(f'ba syms={len(ba_syms)}  gold={len(GOLD)}  需提取={len(NEED)}: {NEED}')

# 建立 var -> [(exchange, member), ...] 索引（只取 00_主力连续 主连）
index = {}
for ex, zp in ZIPS.items():
    if not os.path.exists(zp):
        print('!! missing', zp); continue
    zf = zipfile.ZipFile(zp)
    for m in zf.namelist():
        if '/00_主力连续/' in m and m.endswith('.csv') and '_main_1m' in m:
            var = m.split('/')[0].split('_')[0]
            index.setdefault(var, []).append((ex, m, zp))
    zf.close()

missing = [v for v in NEED if v not in index]
print('提取源缺失(无法补齐):', missing)

# 逐品种提取
summary = {}
for v in NEED:
    members = index.get(v)
    if not members:
        summary[v] = 'NO_SOURCE'; continue
    out = {y: open(os.path.join(BASE, f'{v}_{y}_1m_raw.csv'), 'w', encoding='utf-8', newline='')
           for y in ('2020','2021_2025','2026')}
    header_written = False
    cnt = {'2020':0,'2021_2025':0,'2026':0}
    for ex, m, zp in members:
        with zipfile.ZipFile(zp) as zf:
            with zf.open(m) as f:
                first = True
                for raw in f:
                    line = raw.decode('utf-8-sig', errors='replace')
                    if first:
                        if not header_written:
                            for o in out.values(): o.write(line if line.endswith('\n') else line+'\n')
                            header_written = True
                        first = False
                        continue
                    ystr = line[:4]
                    if not ystr.isdigit():
                        continue
                    yr = int(ystr)
                    if yr == 2020: tgt='2020'
                    elif 2021 <= yr <= 2025: tgt='2021_2025'
                    elif yr == 2026: tgt='2026'
                    else: continue
                    out[tgt].write(line if line.endswith('\n') else line+'\n')
                    cnt[tgt]+=1
    for o in out.values(): o.close()
    total = sum(cnt.values())
    summary[v] = dict(cnt, total=total)
    print(f'  {v}: total={total:,}  2020={cnt["2020"]:,} 2021_2025={cnt["2021_2025"]:,} 2026={cnt["2026"]:,}')

json.dump({'need':NEED,'missing':missing,'summary':summary},
          open(os.path.join(BASE,'extract_1m_summary.json'),'w'), ensure_ascii=False, indent=1)
print('\nDONE -> extract_1m_summary.json')
