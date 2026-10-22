# -*- coding: utf-8 -*-
"""单品种(6候选) × {固定1手, 浮盈加仓≤2手} 在已验证引擎(V5X)上的表现 + 4万本金容量"""
import numpy as np, pandas as pd
import step46_prod_bt as s46
from step49_winrate import replay
from fusion_v5x_engine import V5XConfig, CLUSTER

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
EXCL = s46.EXCLUDE
syms = [s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in EXCL]
data = {s: b for s in syms if (b := s46.build_sym(s, ba, raw, dd, var)) is not None}
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)

cands = {'MA888':'甲醇(MA)','EG888':'乙二醇(EG)','EB888':'苯乙烯(EB)',
         'BR888':'合成橡胶(BR)','SA888':'纯碱(SA)','SI888':'工业硅(SI)'}

def build_blocked():
    return {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}

def metrics(tr, cap=40000.0):
    if not tr: return dict(n=0,net=0,pf=np.nan,win=0,maxdd=0,maxlot=0,
                           final=cap,risk_atr=0,maxloss=0)
    df = pd.DataFrame(tr).sort_values('xdt').reset_index(drop=True)
    eq = cap + df.net.cumsum()
    peak = eq.cummax(); dd = (eq-peak)/peak
    w = df[df.net>0]; l = df[df.net<0]
    gp=w.net.sum(); gl=-l.net.sum()
    # 每手风险 = 2*e_atr*mult（首仓初始止损距离）
    r1 = (2*df.e_atr*df.lots*0).mean()  # placeholder
    risk_lot = (2*df.e_atr).mean()      # 每手 ATR 止损距离(价), 乘 mult 见下
    return dict(n=len(df), net=float(df.net.sum()),
                pf=float(gp/gl) if gl>0 else np.nan,
                win=float(len(w)/len(df)),
                maxdd=float(dd.min()*100),
                maxlot=float(df.lots.max()),
                final=float(eq.iloc[-1]),
                risk_atr=float(risk_lot),
                maxloss=float(df.net.min()))

bm = build_blocked()
print(f"{'品种':14s} {'模式':10s} {'笔数':>5s} {'净利':>10s} {'PF':>6s} {'胜率%':>6s} {'最大手':>5s} {'4万终值':>9s} {'最大回撤%':>8s} {'最大单笔亏':>9s}")
print('-'*100)
res = []
for s,name in cands.items():
    b = data[s]
    vc = s[:-3]
    mult = costmap[vc][2]
    for mode, al in [('固定1手',1),('浮盈加仓',2)]:
        cfg = V5XConfig(); cfg.ADD_MAX_LOTS = al
        tr = replay(s, b, cfg, costmap, tick, bm[s])
        m = metrics(tr)
        res.append((name, mode, m))
        print(f"{name:14s} {mode:10s} {m['n']:5d} {m['net']:10.0f} {str(round(m['pf'],2)) if m['pf']==m['pf'] else 'NA':>6s} "
              f"{m['win']*100:6.1f} {m['maxlot']:5.0f} {m['final']:9.0f} {m['maxdd']:8.1f} {m['maxloss']:9.0f}")
    # 每手平均风险(价) → 元
    cfg=V5XConfig(); cfg.ADD_MAX_LOTS=1
    tr=replay(s,b,cfg,costmap,tick,bm[s])
    if tr:
        d=pd.DataFrame(tr)
        r_atr=(2*d.e_atr*mult).mean()
        print(f"    ↳ 每手平均风险(2ATR止损)≈ {r_atr:,.0f} 元  保证金估算见正文")
print('-'*100)
# 保存
import json
out=[]
for (name,mode,m),s in zip(res,[]):
    pass
dfout=pd.DataFrame([dict(品种=nm,模式=md,**mt) for (nm,md,mt) in res])
dfout.to_csv('step51_single_result.csv', index=False)
print('saved step51_single_result.csv')
