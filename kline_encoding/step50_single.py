import pandas as pd, numpy as np, json
import step46_prod_bt as s46

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
EXCL = s46.EXCLUDE
syms = [s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in EXCL]
data = {s: s46.build_sym(s, ba, raw, dd, var) for s in syms if s46.build_sym(s, ba, raw, dd, var) is not None}
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)

cands = {'MA888':'甲醇','EG888':'乙二醇','EB888':'苯乙烯','BR888':'合成橡胶','SA888':'纯碱','SI888':'工业硅'}
gsets = {
    'V5X全门(含M5)': {'A1S','B3','M1a_pb','M5'},
    '去M5(留P2/P4/P6/M1a)': {'A1S','B3','M1a_pb'},
    '再去M1a(P2/P4/P6)': {'A1S','B3'},
}

def metrics(tr):
    n=len(tr)
    if n==0: return dict(n=0,net=0,pf=np.nan,win=np.nan,maxdd=0,maxlot=0,maxloss=0)
    w=[t for t in tr if t['net']>0]; l=[t for t in tr if t['net']<0]
    gp=sum(t['net'] for t in w); gl=sum(-t['net'] for t in l); net=gp-gl
    pf = gp/gl if gl>0 else np.nan
    eq=40000.0+np.cumsum([t['net'] for t in tr])
    peak=np.maximum.accumulate(eq); dd=(eq-peak)/peak
    return dict(n=n,net=net,pf=pf,win=len(w)/n*100,maxdd=dd.min()*100,
               maxlot=max(t['lots'] for t in tr), maxloss=min(t['net'] for t in tr))

def run_cand(s, flags, addlots):
    s46.ADD_MAX_LOTS = addlots
    vc=s[:-3]
    b=data[s]
    cfgs={'cost':costmap[vc],'tick':tick.get(vc,1.0),
          'blocked_tds':shock.get(s46.CLUSTER.get(vc),set()),
          'entry_mode':'pullback' if vc in s46.P4_SECTORS else 'both_nm'}
    return s46.run_symbol(s,b,cfgs,flags)

print("="*100)
print("一、6候选 × 3门控组合 × {固定1手, 浮盈加仓(≤2手)} 全周期(2015-2026)表现")
print("="*100)
rows=[]
for s,name in cands.items():
    for gname,flags in gsets.items():
        for mode,al in [('固定1手',1),('浮盈加仓',2)]:
            tr=run_cand(s,flags,al)
            m=metrics(tr)
            rows.append((name,gname,mode,m['n'],m['net'],m['pf'],m['win'],m['maxlot'],m['maxdd'],m['maxloss']))
r=pd.DataFrame(rows,columns=['品种','门控','模式','笔数','净利','PF','胜率%','最大手','最大回撤%','最大单笔亏'])
pd.set_option('display.width',220,'display.max_columns',20,'display.float_format',lambda x:f'{x:,.1f}')
print(r.to_string(index=False))

print("\n"+"="*100)
print("二、4万本金容量核查：各候选保证金/手 与 1~2手占用（来自 dim_trading_cost）")
print("="*100)
# 取保证金信息
cc = cost.copy()
cc['vc']=cc['variety_code']
print(cc[['variety_code','exchange_margin_ratio','trade_margin_ratio','min_margin','mult','slip','slip_unit']].head(0))
# 尝试不同列名
print("dim_trading_cost 列:", list(cost.columns))
