# -*- coding: utf-8 -*-
"""step62 — THS 网格重跑的补充分析
1) 已存 trades_ths_THS_V5X.csv / trades_ths_BA_V5X.csv 的逐品种拆解 (gold-13)
2) 「广度」回测：把 THS 网格扩展到全部 60 品种(降门槛, 仅 2026 窗口可用),
   看校正后网格在 2026 样本外的方向性 (化工/黑色/金属/能源各板块)
说明：43 个非金标准品种仅有 2026 Sina 窗口(<=1023根), 200日预热后仅 1-2 月可用,
     故广度运行只反映 2026 末段 OOS 方向, 非完整 IS/OOS。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import step46_prod_bt as s46
import step61_rerun_ths as S

# 解除 1500 门槛以纳入全部 60 品种(2026 窗口)
S.MIN_BARS = 200

GOLD = {'EB','EG','FG','IF','L','MA','PF','PP','PR','PX','SA','TA','UR','V'}

def per_symbol(trades_df):
    if trades_df is None or len(trades_df) == 0:
        return {}
    g = trades_df.groupby('sym')
    return {s: dict(n=int(len(x)), net=float(x.net.sum()),
                    win=float((x.net > 0).mean()),
                    pf=(float(x.loc[x.net>0,'net'].sum()/max(1e-9,-x.loc[x.net<0,'net'].sum()))
                         if (x.net<0).any() else float('nan')))
            for s, x in g}

print('=' * 70)
print('1) gold-13 逐品种拆解: THS(grid校正) vs BA(原整点桶)  V5X')
ths = pd.read_csv('trades_ths_THS_V5X.csv') if os.path.exists('trades_ths_THS_V5X.csv') else None
ba = pd.read_csv('trades_ths_BA_V5X.csv') if os.path.exists('trades_ths_BA_V5X.csv') else None
pt = per_symbol(ths); pb = per_symbol(ba)
print(f'{"sym":6} {"THS_n":>6} {"THS_net":>10} {"THS_pf":>6} | {"BA_n":>6} {"BA_net":>10} {"BA_pf":>6}')
for s in sorted(set(list(pt)+list(pb))):
    a = pt.get(s, {}); b = pb.get(s, {})
    print(f'{s:6} {a.get("n",0):>6} {a.get("net",0):>10,.0f} {a.get("pf",0):>6.2f} | '
          f'{b.get("n",0):>6} {b.get("net",0):>10,.0f} {b.get("pf",0):>6.2f}')

# 2) 广度：全部 60 品种 THS V5X
print('\n' + '=' * 70)
print('2) THS 网格「广度」V5X —— 全部 60 品种 (2026 窗口为主)')
ba_ths, raw_ths = S.load_ths()
ba_ba, raw_ba, dd, var, cost, fac = s46.load_all()
costmap = S.build_cost_fn(cost, var); S.costmap = costmap
S.tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
# 全品种(有成本 + 非CJ)
all_syms = sorted(s for s in ba_ths.symbol.unique()
                  if s[:-3] in S.costmap and s[:-3] not in S.EXCLUDE)
print(f'候选品种数: {len(all_syms)}')
shock = s46.cluster_shock_days({s: S.build_sym_ths(s, ba_ths, raw_ths, dd, var) for s in all_syms})
data = {}
for s in all_syms:
    try:
        b = S.build_sym_ths(s, ba_ths, raw_ths, dd, var)
        if b is not None:
            data[s] = b
    except Exception as e:
        print(f'  {s}: SKIP {e}')
data = S.attach_member_z(data, fac)
flags, scope = S.variant_cfg('V5X')
tb = {sym: S.run_symbol(sym, data[sym], S.cfg_for(sym, scope, shock), flags - {'A2'}) for sym in data}
trades = S.apply_a2(tb)
df = pd.DataFrame(trades)
m = S.metrics(trades, 'THS_ALL_V5X')
print(f'  总 trades={m["n"]}  net={m["net_yuan"]:,.0f}  pf={m["pf"]:.3f}  win={m["win"]:.3f}  mdd={m["mdd_yuan"]:,.0f}')
# 板块汇总
CLUSTER = S.CLUSTER
df['cl'] = df.sym.map(lambda x: CLUSTER.get(x[:-3], '其他'))
print('\n  板块汇总 (2026 窗口广度):')
for cl, x in df.groupby('cl'):
    if len(x):
        print(f'    {cl:6} n={len(x):>4} net={x.net.sum():>10,.0f} win={ (x.net>0).mean():.2f}')
# 逐品种(有交易的)
ps = per_symbol(df)
print('\n  有交易的品种:')
for s in sorted(ps):
    print(f'    {s:6} {ps[s]["n"]:>4} {ps[s]["net"]:>10,.0f} pf={ps[s]["pf"]:.2f}')

df.to_csv('trades_ths_breadth_V5X.csv', index=False, encoding='utf-8-sig')

# 保存广度汇总
breadth = dict(total=m, by_cluster={cl: dict(n=int(len(x)), net=float(x.net.sum()),
                                             win=float((x.net>0).mean())) for cl, x in df.groupby('cl') if len(x)},
              per_sym=ps, n_sym_total=len(all_syms), n_sym_traded=len(ps))
json.dump(breadth, open('results_ths_breadth.json','w',encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
print('\nsaved trades_ths_breadth_V5X.csv + results_ths_breadth.json')
