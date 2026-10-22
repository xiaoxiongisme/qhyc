# -*- coding: utf-8 -*-
"""数据完整性核验（反驳/确认换月bug）：
1) ba 是否存在【年度复发孤立大跳】= 漏调换月的指纹 → 期望 0
2) raw 是否存在【年度复发孤立大跳】= 未调整主连指纹 → 期望 >0（证明 raw 是未调整参考）
3) r=ba/raw 是否存在【向上阶梯】= ba 凭空多跳（坏打印/错拼接）→ 期望 0
结论：ba 已正确后复权(连续)，raw 为未调整参考(设计如此)，无换月bug。
"""
import pandas as pd, numpy as np, collections, json

ba = pd.read_csv('prod50/bar_60m_ba.csv.gz', parse_dates=['ts'])
raw = pd.read_csv('prod50/bar_60m_raw.csv.gz', parse_dates=['ts'])
syms = sorted(ba.symbol.unique())

def recurring_jumps(ts, c, thr=0.02, iso=0.01, win=22, minyr=2):
    ts = pd.DatetimeIndex(ts)
    ret = np.concatenate([[0.0], c[1:]/c[:-1]-1.0])
    cand = []
    for i in range(1, len(ret)):
        if abs(ret[i])>thr and abs(ret[i-1])<iso and (i+1>=len(ret) or abs(ret[i+1])<iso):
            cand.append((ts[i], ret[i]))
    kept=[]
    for d,r in cand:
        yrs=set()
        for d2,r2 in cand:
            if d2.month==d.month and abs((d2-d).days)<=win: yrs.add(d2.year)
        if len(yrs)>=minyr: kept.append(d)
    return len(cand), len(kept), cand

def r_upward(bts, bc, rts, rc, thresh=0.005):
    # 日频 r=ba/raw，向上阶梯(单日 r 跳升>thresh) = ba 凭空多跳
    bb=pd.DataFrame({'ts':pd.DatetimeIndex(bts),'b':bc})
    rr_=pd.DataFrame({'ts':pd.DatetimeIndex(rts),'r':rc})
    df=bb.merge(rr_,on='ts',how='inner').dropna()
    df['d']=df.ts.dt.normalize()
    g=df.groupby('d').agg(b=('b','last'),r=('r','last'))
    rr=g.b/g.r
    rr=rr.replace([np.inf,-np.inf],np.nan).dropna()
    drr=rr.diff().abs()
    return int((drr>thresh).sum())

report={}
bad=[]
for s in syms:
    b=ba[ba.symbol==s].sort_values('ts')
    r=raw[raw.symbol==s].sort_values('ts')
    bc=b['close'].values.astype(float); bts=b['ts'].values
    rc=r['close'].values.astype(float); rts=r['ts'].values
    n_ba_cand,n_ba_kept,_=recurring_jumps(bts,bc)
    n_raw_cand,n_raw_kept,_=recurring_jumps(rts,rc)
    up=r_upward(bts,bc,rts,rc)
    report[s]=dict(ba_cand=n_ba_cand, ba_kept=n_ba_kept,
                   raw_cand=n_raw_cand, raw_kept=n_raw_kept, r_up_step=up)
    if n_ba_kept>0 or up>0:
        bad.append(s)

tot_ba_kept=sum(v['ba_kept'] for v in report.values())
tot_raw_kept=sum(v['raw_kept'] for v in report.values())
tot_up=sum(v['r_up_step'] for v in report.values())
print(f'品种数={len(syms)}')
print(f'ba 年度复发孤立大跳(漏调换月指纹) 合计={tot_ba_kept}  [期望 0]')
print(f'raw 年度复发孤立大跳(未调整指纹) 合计={tot_raw_kept}  [期望 >0, 证明 raw 是未调整参考]')
print(f'r=ba/raw 向上阶梯(ba凭空多跳) 合计={tot_up}  [期望 0]')
print(f'异常品种(ba有复发跳或r向上阶梯): {bad if bad else "无"}')
json.dump(report, open('verify_clean_report.json','w'), ensure_ascii=False, indent=1)
print('saved verify_clean_report.json')
