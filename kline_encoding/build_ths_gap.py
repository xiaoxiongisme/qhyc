# -*- coding: utf-8 -*-
"""
用 akshare(Sina 60m) + 云端 bar_60m_ba/raw 补齐 2026-06-13 ~ (Sina 至 2026-10-21) 的
「同花顺口径」60m 缺口，产出 bar_60m_ths_gap.csv.gz。

已验证（2026-06-05 两端重叠日）：Sina 60m 的 10:00/11:15/14:15/22:00 与从 1m 重建的
同花顺 60m(OHLC) 相差仅 ~1%，即 Sina 60m 就是同花顺网格，只是少了 09:00/21:00 开盘根、
多了 15:00/23:00 尾根。
Sina 的 volume 字段损坏(出现 6/15 等离谱值)，故：
  - OHLC：化学/黑色 4 根(10:00/11:15/14:15/22:00) 取 Sina 原始 × 每日复权比(对齐到复权口径)；
          09:00/21:00 直接取云端 bar_60m_ba(复权、OHLC 完整，且这两根日历小时==会话窗口)。
  - volume/open_interest：4 根 Sina 柱的成交量取自云端 ba 中时间重叠最多的整点桶(近似)，
          09:00/21:00 取 ba 原值。
  - UR 无夜盘：09:00(ba) + 10:00/11:15/14:15(Sina)。
  - IF/PR/SC 会话锚点不同(Sina 网格≠同花顺)，用 Sina 原生 60m 兜底并复权，报告中标注。
"""
import os
import pandas as pd, numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
CHEM = ['EB','EG','FG','L','MA','PF','PP','PX','SA','TA','V']
UR   = ['UR']
FALLBACK = ['IF','PR','SC']

# 各品种的：ba 直接采用的时刻, 取自 Sina 的时刻, 以及 Sina 柱成交量在 ba 中的近似来源桶
SPEC = {
    'chem': dict(ba=['09:00','21:00'],
                 sina=['10:00','11:15','14:15','22:00'],
                 vproxy={'10:00':'10:00','11:15':'13:00','14:15':'14:00','22:00':'22:00'}),
    'ur':   dict(ba=['09:00'],
                 sina=['10:00','11:15','14:15'],
                 vproxy={'10:00':'10:00','11:15':'13:00','14:15':'14:00'}),
}

def load():
    ba  = pd.read_csv(os.path.join(BASE,'prod50/bar_60m_ba.csv.gz'));  ba['ts']=pd.to_datetime(ba['ts'])
    raw = pd.read_csv(os.path.join(BASE,'prod50/bar_60m_raw.csv.gz')); raw['ts']=pd.to_datetime(raw['ts'])
    sina= pd.read_csv(os.path.join(BASE,'sina_60m_raw.csv.gz'));       sina['datetime']=pd.to_datetime(sina['datetime'])
    return ba, raw, sina

def daily_ratio(ba, raw, sym):
    b=ba[ba.symbol==sym][['ts','close']].rename(columns={'close':'ba'})
    r=raw[raw.symbol==sym][['ts','close']].rename(columns={'close':'raw'})
    if len(b)==0 or len(r)==0: return None
    m=b.merge(r,on='ts'); m=m[(m.ba>0)&(m.raw>0)]
    m['date']=m.ts.dt.normalize(); m['ratio']=m.ba/m.raw
    return m.groupby('date')['ratio'].median()

def ratio_for(rs, dt):
    if rs is None or len(rs)==0: return 1.0
    d=pd.Timestamp(dt).normalize()
    if d in rs.index: return float(rs[d])
    return float(rs.reindex(rs.index.union([d])).ffill().loc[d])

def build():
    ba, raw, sina = load()
    rows=[]
    for var in CHEM+UR+FALLBACK:
        sym=var+'888'
        rs=daily_ratio(ba,raw,sym)
        ba_sub=ba[ba.symbol==sym].copy(); ba_sub['hm']=ba_sub.ts.dt.strftime('%H:%M')
        si_sub=sina[sina.symbol==sym].copy(); si_sub['hm']=si_sub.datetime.dt.strftime('%H:%M')
        if var in CHEM: spec=SPEC['chem']
        elif var in UR: spec=SPEC['ur']
        else: spec=None
        # ba 直接采用的根
        if spec is not None:
            for t in spec['ba']:
                bb=ba_sub[ba_sub.hm==t]
                for _,r in bb.iterrows():
                    rows.append((sym,r.ts,r.open,r.high,r.low,r.close,r.volume,
                                 r.open_interest if pd.notna(r.open_interest) else np.nan))
            # Sina 根：OHLC×ratio，volume/OI 取 ba 近似桶
            for t in spec['sina']:
                sb=si_sub[si_sub.hm==t]
                pb=ba_sub[ba_sub.hm==spec['vproxy'][t]]
                for _,r in sb.iterrows():
                    rt=ratio_for(rs,r.datetime)
                    pv=pb.volume.iloc[0] if len(pb) else np.nan
                    po=pb.open_interest.iloc[0] if len(pb) and pd.notna(pb.open_interest.iloc[0]) else np.nan
                    rows.append((sym,r.datetime,r.open*rt,r.high*rt,r.low*rt,r.close*rt,pv,po))
        else:
            # fallback: Sina 原生，去尾根，复权
            drop={'15:00','23:00'}
            for _,r in si_sub.iterrows():
                if r.hm in drop: continue
                rt=ratio_for(rs,r.datetime)
                rows.append((sym,r.datetime,r.open*rt,r.high*rt,r.low*rt,r.close*rt,r.volume,r.hold))
    df=pd.DataFrame(rows,columns=['symbol','ts','open','high','low','close','volume','open_interest'])
    df=df.sort_values(['symbol','ts']).reset_index(drop=True)
    df=df[df.ts>='2026-06-13'].reset_index(drop=True)
    outp=os.path.join(BASE,'bar_60m_ths_gap.csv.gz'); df.to_csv(outp,index=False)
    print('SAVED',outp,'rows=',len(df),'symbols=',df.symbol.nunique())
    for sym in sorted(df.symbol.unique()):
        s=df[df.symbol==sym]; hm=sorted(s.ts.dt.strftime('%H:%M').unique())
        pd_=s.groupby(s.ts.dt.date).size().mode().iloc[0]
        print(f'  {sym}: bars/day~{pd_} hhmm={hm} {s.ts.min().date()}..{s.ts.max().date()}')
    return df

if __name__=='__main__':
    build()
