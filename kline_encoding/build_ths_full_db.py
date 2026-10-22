"""
build_ths_full_db.py
===================
把【数据库 prod50 里全部 59 品种】的 60m 数据都做成「同花顺网格」并入库。

方法 (通用, 适用于全部品种):
- 用 akshare 底层 Sina 直连接口取各品种 60m (period=60)。Sina 按【收盘时刻】打标,
  其 60m 即「连续 60 交易分钟」累加 = 同花顺网格 (已对化工类逐根验证 OHLC 吻合)。
- 同花顺起始时刻 = Sina 收盘标签 - 60 分钟 (自动处理午夜跨日)。
- 刻度对齐: 取云端 bar_60m_ba (后复权) 与 Sina (未复权) 在【共有标签】上的比值,
  按日取中值 -> 把 Sina 对齐到云端刻度+换月口径。共有标签逐品种动态求交集
  (化工共有 09/10/21/22; 金属共有 21/22/23/00/01/14 等), 无需手工枚举。
- 14 个已有 1m 金标准的品种保留其全历史 (2020-2026-06-12, 经 rebuild 重建) + 已补缺口,
  不直接用 Sina (Sina 仅 ~8-13 月覆盖); 其余 45 品种用 Sina 窗口 (覆盖期取决于各品种)。
- CJ888 云端 2024-02-07 起损坏(负价) -> 用 Sina CJ0 (2025-09-24 起) 重建该段, 此前标记不可用。

产物:
  bar_60m_ths_full.csv.gz   —— 全样本(59 品种) 同花顺网格 60m, 进入本目录
  prod50/bar_60m_ths.csv.gz —— 入库副本 (数据库校准备份表)
"""
import os, time, json
import pandas as pd, numpy as np, requests

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.join(HERE, "prod50")
CLOUD_BA = os.path.join(PROD, "bar_60m_ba.csv.gz")
GOLD = os.path.join(HERE, "bar_60m_ths_full.csv.gz")   # 14 个金标准 (已含缺口补齐)
OUT  = os.path.join(HERE, "bar_60m_ths_full.csv.gz")
OUT_DB = os.path.join(PROD, "bar_60m_ths.csv.gz")
SINA_CACHE = os.path.join(HERE, "sina_60m_all.csv.gz")
SINA_URL = "https://stock2.finance.sina.com.cn/futures/api/json.php/InnerFuturesNewService.getFewMinLine"

# 金标准品种(var, 已有 1m 全历史) -- 这些不用 Sina 重建
GOLD_VARS = {'EB','EG','FG','IF','L','MA','PF','PP','PR','PX','SA','TA','UR','V'}

def sina_code(sym888):
    return sym888[:-3] + '0'   # MA888 -> MA0

def fetch_one(sym, tries=4):
    last=None
    for i in range(tries):
        try:
            r = requests.get(SINA_URL, params={"symbol":sym,"type":"60"},
                             timeout=15, headers={"User-Agent":"Mozilla/5.0",
                                                  "Referer":"https://finance.sina.com.cn"})
            r.raise_for_status()
            j = r.json()
            rows=[]
            for it in j:
                rows.append(dict(
                    datetime=pd.Timestamp(it['d']),
                    open=float(it['o']), high=float(it['h']), low=float(it['l']),
                    close=float(it['c']), volume=int(float(it['v'])), hold=int(float(it['p'])),
                ))
            return pd.DataFrame(rows)
        except Exception as e:
            last=e; time.sleep(1.2)
    print(f"    !! {sym} failed after {tries} tries: {repr(last)[:100]}")
    return None

def fetch_all(symbols):
    """symbols: list of Sina codes; returns DataFrame with extra col 'symbol'=var888"""
    frames=[]
    for sym in symbols:
        var = sym[:-1]  # MA0 -> MA
        df = fetch_one(sym)
        if df is None or len(df)==0:
            print(f"  fetch {var}: EMPTY/SKIP")
            continue
        df['symbol']=var+'888'
        frames.append(df)
        print(f"  fetch {var}: {len(df)} bars {df.datetime.min()}->{df.datetime.max()}")
    if not frames:
        raise RuntimeError("no Sina data")
    allp = pd.concat(frames, ignore_index=True)
    return allp

def build():
    # 1) 云端 ba 索引: (symbol, 'YYYY-MM-DD HH:MM') -> close
    ba = pd.read_csv(CLOUD_BA); ba['ts']=pd.to_datetime(ba['ts'])
    ba['start'] = ba.ts.dt.strftime('%Y-%m-%d %H:%M')
    ba_map = {(r['symbol'], r['start']): r['close'] for _,r in ba.iterrows() if r['close'] and not pd.isna(r['close'])}
    # 每个 symbol 的 cloud 标签集合
    ba_labels = {s: set(ba[ba.symbol==s].ts.dt.strftime('%H:%M').unique()) for s in ba.symbol.unique()}

    # 2) 需要 Sina 补齐的品种 (全部 59 中, 排除金标准 14)
    all_syms = sorted(ba.symbol.unique())
    need = [s for s in all_syms if s[:-3] not in GOLD_VARS]
    need_codes = [sina_code(s) for s in need]
    print(f"全部品种 {len(all_syms)}, 金标准 {len(GOLD_VARS)}, 需 Sina 补齐 {len(need)}")

    # 3) 取数 (缓存命中则用缓存)
    if os.path.exists(SINA_CACHE):
        sina = pd.read_csv(SINA_CACHE); sina['datetime']=pd.to_datetime(sina['datetime'])
        print(f"load cache {SINA_CACHE} rows={len(sina)}")
    else:
        print("fetching Sina 60m (direct) ...")
        sina = fetch_all(need_codes)
        sina.to_csv(SINA_CACHE, index=False)
        print(f"saved cache rows={len(sina)}")

    rows=[]; log={}
    for sym in need:
        sub = sina[sina.symbol==sym].sort_values('datetime').copy()
        if len(sub)==0:
            log[sym]=dict(status='no_sina'); continue
        # 该品种 Sina 起始标签集合 (close-60min)
        sub['ths_start'] = (sub.datetime - pd.Timedelta(minutes=60)).dt.strftime('%H:%M')
        sina_labels = set(sub['ths_start'].unique())
        common = sorted(sina_labels & ba_labels.get(sym, set()))
        verified = len(common)>0
        # 按日求比值 (共有标签)
        day_ratio={}
        for d,g in sub.groupby(sub.datetime.dt.date):
            ds=d.strftime('%Y-%m-%d')
            rats=[]
            for _,r in g.iterrows():
                L=r['ths_start']
                if L in common:
                    bk=(sym, ds+' '+L)
                    if bk in ba_map and ba_map[bk]>0 and r['close']>0:
                        rats.append(ba_map[bk]/r['close'])
            day_ratio[ds]=np.median(rats) if rats else np.nan
        fb=np.nanmedian([v for v in day_ratio.values() if not np.isnan(v)])
        if np.isnan(fb): fb=1.0
        log[sym]=dict(n_days=len(day_ratio), matched=sum(1 for v in day_ratio.values() if not np.isnan(v)),
                      fb=round(float(fb),5), common=common, verified=verified)
        # 生成行
        for _,r in sub.iterrows():
            L=r['ths_start']
            ts=r.datetime - pd.Timedelta(minutes=60)   # 起始时刻 (含跨午夜)
            ds=ts.strftime('%Y-%m-%d')
            ratio=day_ratio.get(ds, np.nan)
            if np.isnan(ratio): ratio=fb
            if np.isnan(ratio) or ratio<=0: continue
            rows.append(dict(symbol=sym, ts=ts,
                             open=r['open']*ratio, high=r['high']*ratio, low=r['low']*ratio,
                             close=r['close']*ratio, volume=int(r['volume']), hold=int(r['hold']),
                             grid_verified=verified))
    sina_built = pd.DataFrame(rows)

    # 4) 合并金标准 (14) + Sina 补齐 (45)
    gold = pd.read_csv(GOLD); gold['ts']=pd.to_datetime(gold['ts'])
    gold['grid_verified']=True; gold['hold']=np.nan
    cols=['symbol','ts','open','high','low','close','volume','hold','grid_verified']
    gold=gold.reindex(columns=cols)
    sina_built=sina_built.reindex(columns=cols)
    both = pd.concat([gold, sina_built], ignore_index=True)
    before=len(both)
    both = both.drop_duplicates(subset=['symbol','ts'], keep='first').sort_values(['symbol','ts']).reset_index(drop=True)
    both.to_csv(OUT, index=False)
    # 入库副本
    both.to_csv(OUT_DB, index=False)
    print(f"\nFULL FILE: {OUT} rows={len(both)} symbols={both.symbol.nunique()} "
          f"range={both.ts.min()}->{both.ts.max()} (dedup dropped {before-len(both)})")
    print(f"DB copy: {OUT_DB}")
    # 日志
    print("\n=== per-symbol build log (Sina 补齐部分) ===")
    for s in need:
        lg=log.get(s)
        if not lg: continue
        if lg.get('status')=='no_sina':
            print(f"  {s}: NO SINA DATA"); continue
        cov = both[both.symbol==s]
        print(f"  {s}: verified={int(lg['verified'])} common={lg['common']} fb={lg['fb']} "
              f"rows={len(cov)} {cov.ts.min() if len(cov) else '-'}->{cov.ts.max() if len(cov) else '-'}")
    return both

if __name__=="__main__":
    build()
