# -*- coding: utf-8 -*-
"""校验 step65 产出的全样本同花顺 60m 网格：
  1) 结构质量: NaN OHLC / high<low / 非正价
  2) 刻度对齐: 与 bar_60m_ba 在【共有标签】上的相对误差 (应 <1%)
  3) 会话结构: sess_last / sess_first 比率合理
  4) 覆盖: 起止 / 根数
"""
import numpy as np, pandas as pd, json

BASE = 'E:/抖音分析/2026-10-16-19-50-44/kline_encoding'
ths = pd.read_csv(f'{BASE}/bar_60m_ths_full_rebuilt.csv.gz', parse_dates=['ts'])
ba = pd.read_csv(f'{BASE}/prod50/bar_60m_ba.csv.gz', parse_dates=['ts'])
print(f'THS rows={len(ths):,} syms={ths.symbol.nunique()}  range {ths.ts.min()}..{ths.ts.max()}')
print(f'BA  rows={len(ba):,} syms={ba.symbol.nunique()}')

# 1) 结构质量
rep = {}
ohlc = ['open','high','low','close']
nan_ohlc = ths[ohlc].isna().any(axis=1).sum()
hl_bad = (ths.high < ths.low).sum()
nonpos = (ths[ohlc] <= 0).any(axis=1).sum()
zero_vol = (ths.volume == 0).sum()
print(f'\n[结构] NaN_OHLC={nan_ohlc}  high<low={hl_bad}  非正价={nonpos}  零量={zero_vol} ({zero_vol/len(ths)*100:.3f}%)')
rep['struct'] = dict(nan_ohlc=int(nan_ohlc), hl_bad=int(hl_bad), nonpos=int(nonpos), zero_vol=int(zero_vol))

# 2) 刻度对齐 (共有标签)
ba2 = ba.rename(columns={'close':'ba_close'})[['symbol','ts','ba_close']]
m = ths.merge(ba2, on=['symbol','ts'], how='inner')
m = m[m.ba_close > 0]
m['rel'] = (m.close - m.ba_close) / m.ba_close
print(f'\n[对齐] 共有标签匹配行数={len(m):,}')
align = {}
for s, g in m.groupby('symbol'):
    err = g['rel'].abs()
    align[s] = dict(n=len(g), mean=round(float(err.mean()*100),4),
                    max=round(float(err.max()*100),4),
                    p99=round(float(err.quantile(0.99)*100),4))
align_df = pd.DataFrame(align).T.sort_values('mean')
print('  对齐最差 12 个品种 (mean% / max% / n):')
print(align_df.tail(12).to_string())
print('  对齐最好示例:')
print(align_df.head(4).to_string())
worst = align_df[align_df['max'] > 1.0]
print(f'\n  max误差>1% 的品种数: {len(worst)} -> {list(worst.index)}')
rep['align'] = dict(n_matched=int(len(m)),
                    n_over_1pct=int(len(worst)),
                    worst=align_df.tail(8).round(4).to_dict('index'))

# 3) 会话结构 (gap>=360min 判 sess_last)
gaps = ths.sort_values(['symbol','ts']).groupby('symbol')['ts'].diff().dt.total_seconds()/60.0
ths['_gap'] = gaps
sl_rate = ths.groupby('symbol').apply(lambda g: (g['_gap'].shift(-1)>=360).mean())
sf_rate = ths.groupby('symbol').apply(lambda g: (g['_gap']>=360).mean())
print('\n[会话] sess_last 率 (样例):')
for s in ['MA888','AG888','AU888','A888','AP888','SC888']:
    print(f'  {s}: sess_last={sl_rate.get(s,np.nan):.3f} sess_first={sf_rate.get(s,np.nan):.3f}')

# 4) 覆盖
cov = ths.groupby('symbol').agg(n=('ts','size'), start=('ts','min'), end=('ts','max'))
print('\n[覆盖] 品种数=%d  最小根数=%d  根数<1500:%d' % (
    len(cov), int(cov.n.min()), int((cov.n<1500).sum())))
rep['coverage'] = dict(n_sym=len(cov), min_bars=int(cov.n.min()),
                       below1500=int((cov.n<1500).sum()))

json.dump(rep, open(f'{BASE}/validate_ths_full_rebuilt.json','w'), ensure_ascii=False, indent=1)
print('\nsaved validate_ths_full_rebuilt.json')
