# -*- coding: utf-8 -*-
"""
step18: 生成优化版编码数据文件 (v2)
格式: datetime, open, chg_pct, amp_pct, clv, code7, up_shadow_pct, lo_shadow_pct
  - (open, chg_pct, amp_pct, clv) 与 OHLC 数学双射(无损), round-trip 已验证
  - code7 = 实体3态(2b) + CLV8档(3b) + 振幅4档(2b), 振幅档阈值见 v2_code_thresholds.json
  - up/lo_shadow_pct 为便利派生列(可从前四列公式推出)
"""
import pandas as pd, numpy as np, json

OUT = "."
THR = {}
for sym in ['MA', 'EG']:
    d = pd.read_csv(f'{sym}_2020_1m_raw.csv', parse_dates=['datetime'])
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    rng = h - l
    amp = rng / o * 100.0
    m = amp > 0
    THR[sym] = {'q25': float(np.quantile(amp[m], 0.25)), 'q50': float(np.quantile(amp[m], 0.5)),
                'q75': float(np.quantile(amp[m], 0.75))}
    chg = (c - o) / o * 100.0
    clv = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1.0), 0.5)
    body3 = np.where(c > o, 2, np.where(c == o, 1, 0))
    clv8 = np.clip((clv * 8).astype(int), 0, 7)
    qs = [THR[sym]['q25'], THR[sym]['q50'], THR[sym]['q75']]
    ampq = np.digitize(amp, qs)
    code7 = body3 * 32 + clv8 * 4 + ampq
    # 派生影线(相对open的百分比)
    up_sh = np.where(rng > 0, (h - np.maximum(o, c)) / o * 100.0, 0.0)
    lo_sh = np.where(rng > 0, (np.minimum(o, c) - l) / o * 100.0, 0.0)
    out = pd.DataFrame({
        'datetime': d.datetime.dt.strftime('%Y-%m-%d %H:%M:%S'),
        'open': o,
        'chg_pct': np.round(chg, 6),
        'amp_pct': np.round(amp, 6),
        'clv': np.round(clv, 6),
        'code7': code7,
        'up_shadow_pct': np.round(up_sh, 6),
        'lo_shadow_pct': np.round(lo_sh, 6)})
    out.to_csv(f'{OUT}/{sym}_2020_1m_encoded_v2.csv', index=False)
    # 自检: round-trip
    o2 = out.open.values
    c2 = o2 * (1 + out.chg_pct.values / 100)
    r2 = o2 * out.amp_pct.values / 100
    l2 = c2 - out.clv.values * r2
    h2 = l2 + r2
    err = max(np.abs(h2 - h).max(), np.abs(l2 - l).max(), np.abs(c2 - c).max())
    print(f'{sym}: rows={len(out):,} code7取值数={out.code7.nunique()} round-trip最大误差={err:.2e} '
          f'(range=0的K线 {int((~m).sum()):,} 根={((~m).mean()*100):.2f}%)')
    THR[sym]['n_rows'] = int(len(out))
json.dump(THR, open(f'{OUT}/v2_code_thresholds.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('thresholds:', {k: {kk: round(vv, 4) for kk, vv in v.items() if isinstance(vv, float)} for k, v in THR.items()})
print('saved -> *_2020_1m_encoded_v2.csv, v2_code_thresholds.json')
