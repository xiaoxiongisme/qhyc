# -*- coding: utf-8 -*-
"""step76 数据不完整根因审计（请求#2）
1) 用 .bak_nan 还原 203 根前导 NaN 棒：品种分布、是否全部落在序列最前段、首根 ts。
2) 对受影响品种，溯源 _sym1m 上游 1m：首根 NaN 对应交易日的 1m 棒数 vs 该品种常态日棒数，
   证明根因是「上游 1m 连续序列在上市/换月拼接处前导稀疏」，而非后复权步骤。
3) 跨表完整性：当前已清洗表（ba/raw/ths/daily）的重复 ts、high<low、零量棒、raw 负价；
   factor_value 的 NaN z_value 覆盖。
"""
import pandas as pd, numpy as np, os, json, glob

DATA = 'prod50'
OUT = 'step76_data_audit.json'

# ---------- 1) 还原 203 根 NaN 棒 ----------
ba_bak = pd.read_csv(f'{DATA}/bar_60m_ba.csv.gz.bak_nan', compression='gzip', parse_dates=['ts'])
ohlc = ['open', 'high', 'low', 'close']
nan_mask = ba_bak[ohlc].isna().any(axis=1)
nan_df = ba_bak[nan_mask].copy().sort_values(['symbol', 'ts'])
print(f'[1] backup 总行 {len(ba_bak)}，NaN-OHLC 棒 {len(nan_df)}，涉及 {nan_df.symbol.nunique()} 个品种')

nan_info = {}
# 每个品种在序列中的前导位置：NaN 棒的最大行索引 / 该品种总行数
g = ba_bak.groupby('symbol')
for sym, sub in nan_df.groupby('symbol'):
    total = len(g.get_group(sym))
    # 该品种全序列里 NaN 棒的行号范围（按 ts 排序后的位置）
    idx = sub.index.tolist()
    pos = ba_bak.index.get_indexer(idx)
    # 用 ts 排序后的位次
    srt = ba_bak[ba_bak.symbol == sym].sort_values('ts').reset_index(drop=True)
    nan_pos = srt.index[srt[ohlc].isna().any(axis=1)].tolist()
    nan_info[sym] = dict(
        n_nan=len(nan_pos),
        first_nan_ts=str(sub.ts.min()),
        last_nan_ts=str(sub.ts.max()),
        max_lead_pos=int(max(nan_pos)),
        total_bars=total,
        all_leading=(max(nan_pos) <= 15),   # 全部落在最前 15 根
    )
n_leading = sum(1 for v in nan_info.values() if v['all_leading'])
print(f'    全部为「序列最前 15 根内」的品种数: {n_leading}/{len(nan_info)}')

# ---------- 2) 溯源 _sym1m 上游 1m ----------
print('[2] 溯源上游 1m（受影响品种前导日 vs 常态日棒数）...')
sym1m_dir = '_sym1m'
trace = {}
for sym in sorted(nan_info.keys()):
    f = f'{sym1m_dir}/{sym}.csv'
    if not os.path.exists(f):
        trace[sym] = dict(src='MISSING')
        continue
    # 只读日期列，极大提速
    d = pd.read_csv(f, header=0, usecols=[0], names=['dt'], parse_dates=['dt'])
    d['date'] = d.dt.dt.strftime('%Y-%m-%d')
    daily_cnt = d.groupby('date').size()
    # 首根 NaN 对应交易日
    first_ts = pd.to_datetime(nan_info[sym]['first_nan_ts'])
    fdate = first_ts.strftime('%Y-%m-%d')
    # 取该品种首根 NaN 当天及前后各 1 天
    around = daily_cnt[[x for x in daily_cnt.index if x in (
        (first_ts - pd.Timedelta(days=1)).strftime('%Y-%m-%d'), fdate,
        (first_ts + pd.Timedelta(days=1)).strftime('%Y-%m-%d'))]]
    typ = int(daily_cnt.median())
    firstday = int(daily_cnt.get(fdate, 0))
    trace[sym] = dict(
        first_nan_date=fdate,
        firstday_1m=int(firstday),
        typical_1m=typ,
        ratio=round(firstday / typ, 3) if typ else None,
        around={k: int(v) for k, v in around.items()},
    )
print(f'    已溯源 {sum(1 for v in trace.values() if "firstday_1m" in v)} 个品种')

# ---------- 3) 跨表完整性（当前已清洗） ----------
print('[3] 跨表完整性检查...')
integ = {}
files = {
    'bar_60m_ba': f'{DATA}/bar_60m_ba.csv.gz',
    'bar_60m_raw': f'{DATA}/bar_60m_raw.csv.gz',
    'bar_60m_ths': f'{DATA}/bar_60m_ths.csv.gz',
    'daily_bar': f'{DATA}/daily_bar.csv.gz',
}
for name, f in files.items():
    df = pd.read_csv(f, parse_dates=['ts'] if 'ts' in pd.read_csv(f, nrows=1).columns else ['trade_date'])
    cols = list(df.columns)
    rec = dict(rows=int(len(df)), cols=cols)
    if 'ts' in cols:
        dup = df.duplicated(['symbol', 'ts']).sum()
        rec['dup_sym_ts'] = int(dup)
    if {'open', 'high', 'low', 'close'}.issubset(cols):
        rec['high_lt_low'] = int((df.high < df.low).sum())
    if 'volume' in cols:
        rec['zero_vol'] = int((df.volume == 0).sum())
    if 'close' in cols:
        rec['nonpos_close'] = int((df.close <= 0).sum())
    integ[name] = rec
    print(f'    {name}: {rec}')

# factor_value NaN
fac = pd.read_csv(f'{DATA}/factor_value.csv.gz', parse_dates=['trade_date', 'available_at'])
print(f'[3b] factor_value: rows={len(fac)} factors={fac.factor_id.nunique()}')
fz = fac[fac.factor_id == 'member_net_z']
nanz = fz[fz.z_value.isna()]
integ['factor_value'] = dict(
    rows=int(len(fac)),
    member_net_z_rows=int(len(fz)),
    member_net_z_nan=int(len(nanz)),
    syms_with_member=int(fz.symbol.nunique()),
    syms_member_all_nan=int(fz.groupby('symbol').z_value.apply(lambda s: s.isna().all()).sum()),
)
print(f'    factor_value member_net_z NaN={len(nanz)}, 全 NaN 品种数={integ["factor_value"]["syms_member_all_nan"]}')

# 汇总
summary = dict(
    backup_nan_total=int(len(nan_df)),
    backup_nan_syms=int(nan_df.symbol.nunique()),
    all_leading_syms=n_leading,
    root_cause=('上游1m主连序列在上市/换月拼接处前导稀疏，60m重采样首桶为空/部分 '
                '-> OHLC=NaN；后复权是价格平移，不造也不补棒，故与后复权无关'),
)
out = dict(summary=summary, nan_per_sym=nan_info, trace_1m=trace, integrity=integ)
json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
print(f'[done] -> {OUT}')
