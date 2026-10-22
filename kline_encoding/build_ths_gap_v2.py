"""
build_ths_gap_v2.py
用 akshare 底层的 Sina 直连接口补齐 同花顺网格 60m 缺口 (2026-06-13 ~ 2026-10-16)。

关键认知 (user 纠正):
- Sina 60m 按【收盘时刻】打标: 标 "10:00" = 9:00->10:00 那根开盘根。并不缺数据。
- 收盘标签->同花顺起始标签映射 (逐根验证 OHLC 吻合, 仅差复权比):
    化工/UR : 10:00->09:00, 11:15->10:00, 14:15->11:15, 15:00->14:15, 22:00->21:00, 23:00->22:00
    IF      : 10:30->09:30, 11:30->10:30, 14:00->13:00, 15:00->14:00
- 复权比 = cloud_ba / sina, 日内恒定。用双方共有干净根(09:00/21:00/22:00 起始)按日取中值,
  套到当天全部根 -> Sina(未复权/MA0) 对齐 cloud(后复权/MA888) 刻度+换月口径。
- PR/SC 标签体系不同, 输出但 grid_verified=False, 复权比缺失时回退 1.0(原始未复权)。

抓取: 直连 Sina JSON 接口, 每品种 timeout=15s + 重试3次, 增量落盘避免卡死。
产物: bar_60m_ths_gap.csv.gz
"""
import pandas as pd, numpy as np, requests, os, json, time

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD_BA = os.path.join(HERE, "prod50", "bar_60m_ba.csv.gz")
SINA_CACHE = os.path.join(HERE, "sina_60m_raw.csv.gz")
OUT = os.path.join(HERE, "bar_60m_ths_gap.csv.gz")
SINA_URL = "https://stock2.finance.sina.com.cn/futures/api/json.php/InnerFuturesNewService.getFewMinLine"

GAP_START = "2026-06-13"
GAP_END   = "2026-10-16"

SYMS = {
    'EB':'EB0','EG':'EG0','FG':'FG0','IF':'IF0','L':'L0','MA':'MA0',
    'PF':'PF0','PP':'PF0','PR':'PR0','PX':'PX0','SA':'SA0',
    'TA':'TA0','UR':'UR0','V':'V0',
}  # SC 剔除：跨午夜且 Sina 标签无法映射同花顺 SC 网格
CHEM = ['EB','EG','FG','L','MA','PF','PP','PX','SA','TA','V']
REMAP = {
    'chem': {"10:00":"09:00","11:15":"10:00","14:15":"11:15","15:00":"14:15","22:00":"21:00","23:00":"22:00"},
    'ur':   {"10:00":"09:00","11:15":"10:00","14:15":"11:15","15:00":"14:15"},
    'if':   {"10:30":"09:30","11:30":"10:30","14:00":"13:00","15:00":"14:00"},
}
CLEAN = ['09:00','21:00','22:00']  # 同花顺起始标签, Sina 与 cloud 都有的干净根

def fetch_one(sym, tries=3):
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
            last=e; time.sleep(1.5)
    print(f"    !! {sym} failed after {tries} tries: {repr(last)[:120]}")
    return None

def fetch_sina():
    frames=[]
    for var, s in SYMS.items():
        df = fetch_one(s)
        if df is None or len(df)==0:
            print(f"  fetch {var}: EMPTY/SKIP")
            continue
        df['symbol']=var+'888'
        frames.append(df)
        print(f"  fetch {var}: {len(df)} bars, {df.datetime.min()} -> {df.datetime.max()}, vol0={df.volume.iloc[0]}")
    if not frames:
        raise RuntimeError("no Sina data fetched")
    allp = pd.concat(frames, ignore_index=True)
    allp.to_csv(SINA_CACHE, index=False)
    print(f"  saved {SINA_CACHE} rows={len(allp)}")
    return allp

def build():
    if not os.path.exists(SINA_CACHE):
        print("re-fetching Sina 60m (direct) ...")
        sina = fetch_sina()
    else:
        sina = pd.read_csv(SINA_CACHE); sina['datetime']=pd.to_datetime(sina['datetime'])
    ba = pd.read_csv(CLOUD_BA); ba['ts']=pd.to_datetime(ba['ts'])
    ba['start'] = ba.ts.dt.strftime('%Y-%m-%d %H:%M')  # full "YYYY-MM-DD HH:MM"
    ba_map = {}  # (symbol, "YYYY-MM-DD HH:MM") -> close
    for _,r in ba.iterrows():
        ba_map[(r['symbol'], r['start'])] = r['close']

    DAY_SESS = {'09:00','10:00','11:15','14:15'}
    NIGHT_SESS = {'21:00','22:00'}
    rows=[]; sym_log={}
    for var in SYMS:
        sub = sina[sina.symbol==var+'888'].sort_values('datetime').copy()
        if var in CHEM: rem=REMAP['chem']
        elif var=='UR': rem=REMAP['ur']
        elif var=='IF': rem=REMAP['if']
        else: rem=None
        verified = rem is not None

        day_ratio={}; night_ratio={}
        for d,g in sub.groupby(sub.datetime.dt.date):
            ds=d.strftime('%Y-%m-%d')
            dr=[]; nr=[]
            for _,r in g.iterrows():
                cl=r.datetime.strftime('%H:%M')
                ths_start = rem.get(cl) if rem else (r.datetime-pd.Timedelta(minutes=60)).strftime('%H:%M')
                bk=(var+'888', ds+' '+ths_start)
                if bk in ba_map and ba_map[bk]>0 and r['close']>0:  # dynamic clean-match (label exists in cloud ba)
                    rt=ba_map[bk]/r['close']
                    (dr if ths_start in DAY_SESS else nr).append(rt)
            day_ratio[ds]=np.median(dr) if dr else np.nan
            night_ratio[ds]=np.median(nr) if nr else np.nan
        fb_day=np.nanmedian([v for v in day_ratio.values() if not np.isnan(v)])
        fb_night=np.nanmedian([v for v in night_ratio.values() if not np.isnan(v)])
        if np.isnan(fb_day): fb_day=1.0
        if np.isnan(fb_night): fb_night=1.0
        sym_log[var+'888']=dict(n_days=len(day_ratio),
                          day_matched=sum(1 for v in day_ratio.values() if not np.isnan(v)),
                          night_matched=sum(1 for v in night_ratio.values() if not np.isnan(v)),
                          fb_day=round(float(fb_day),5), fb_night=round(float(fb_night),5))
        # parse 'HH:MM' -> minutes since midnight
        def hm(s): h,m=s.split(':'); return int(h)*60+int(m)

        for _,r in sub.iterrows():
            cl=r.datetime.strftime('%H:%M')
            ths_start = rem.get(cl) if rem else (r.datetime-pd.Timedelta(minutes=60)).strftime('%H:%M')
            mins=hm(ths_start)
            ts=pd.Timestamp(r.datetime.date())+pd.Timedelta(minutes=mins)
            ds=ts.strftime('%Y-%m-%d')
            if not (GAP_START<=ds<=GAP_END): continue
            if ths_start in NIGHT_SESS:
                ratio=night_ratio.get(ds, np.nan)
                if np.isnan(ratio): ratio=fb_night
            else:
                ratio=day_ratio.get(ds, np.nan)
                if np.isnan(ratio): ratio=fb_day
            if np.isnan(ratio) or ratio<=0: continue
            rows.append(dict(symbol=var+'888', ts=ts,
                             open=r['open']*ratio, high=r['high']*ratio, low=r['low']*ratio,
                             close=r['close']*ratio, volume=int(r['volume']), hold=int(r['hold']),
                             grid_verified=verified))
    df=pd.DataFrame(rows).sort_values(['symbol','ts']).reset_index(drop=True)
    df.to_csv(OUT, index=False)
    print(f"\nGAP FILE: {OUT}")
    print(f"  rows={len(df)} symbols={df.symbol.nunique()} range={df.ts.min()} -> {df.ts.max()}")
    print(f"  grid_verified per symbol:")
    for s,v in df.groupby('symbol')['grid_verified'].first().items():
        lg=sym_log[s]
        print(f"    {s}: verified={int(v)}  day={lg['day_matched']}/{lg['n_days']} night={lg['night_matched']}/{lg['n_days']} fb_d={lg['fb_day']} fb_n={lg['fb_night']}")
    return df

if __name__=="__main__":
    build()
