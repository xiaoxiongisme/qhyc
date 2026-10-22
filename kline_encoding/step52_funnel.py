"""V5X 信号漏斗诊断：从原始方向翻转 → 最终成交，量化每道门砍掉多少。
使用已验证引擎 fusion_v5x_engine.py 的 DIAGNOSE 开关（默认关，不影响自检）。
"""
import numpy as np
import pandas as pd
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CLUSTER, SymbolEngine, DailyCtx, CostCfg

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
EXCL = s46.EXCLUDE
syms = [s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in EXCL]
data = {s: s46.build_sym(s, ba, raw, dd, var) for s in syms}
data = {s: b for s, b in data.items() if b is not None}
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)

funnel, mz_cov = {}, {}
for s, b in data.items():
    vc = s[:-3]
    fee_row, slip, mult = costmap[vc]
    cfg = V5XConfig(); cfg.DIAGNOSE = True
    eng = SymbolEngine(s, cfg, CostCfg(fee_row, slip, mult, tick.get(vc, 1.0)))
    cl = CLUSTER.get(vc)
    blocked = shock.get(cl, set())
    sl_hours = frozenset(int(x) for x in b.loc[b.sess_last, 'hour'].unique())
    prev_td = [None]
    for row in b.itertuples():
        td = row.td
        if td != prev_td[0]:
            eng.set_daily_ctx(DailyCtx(
                td=td, hvol=bool(row.hvol),
                member_z=float(row.member_z) if not pd.isna(row.member_z) else np.nan,
                blocked=td in blocked, sess_last_hours=sl_hours))
            prev_td[0] = td
        eng.on_bar(row.ts, float(row.open), float(row.high), float(row.low),
                   float(row.close), float(row.volume), float(row.raw_c),
                   allow=bool(row.allow))
    st = dict(eng._stat)
    funnel[s] = st
    mz = b['member_z'].dropna()
    mz_cov[s] = float((mz.abs() >= 1.0).mean()) if len(mz) else np.nan

# 实际成交（来自干净明细）
act = pd.read_csv('trades49_V5X.csv').groupby('sym').size().to_dict()

rows = []
for s, st in funnel.items():
    bars = st['bars']; flip = st['flip']
    raw = st['raw']; p2 = st['p2']; blk = st['blk']; m1a = st['m1a']; m5 = st['m5']
    # 每道门砍掉比例（相对上一阶段）
    c_p2 = (raw - p2) / raw if raw else 0
    c_blk = (p2 - blk) / p2 if p2 else 0
    c_m1a = (blk - m1a) / blk if blk else 0
    c_m5 = (m1a - m5) / m1a if m1a else 0
    rows.append(dict(
        sym=s, vc=s[:-3], bars=bars, flip=flip,
        flip_per_yr=flip / (bars / 252 / 4) if bars else 0,  # 4 bar/日
        raw=raw, p2=p2, blk=blk, m1a=m1a, m5=m5,
        c_p2=c_p2, c_blk=c_blk, c_m1a=c_m1a, c_m5=c_m5,
        mz_cov=mz_cov[s], actual=act.get(s, 0)))
df = pd.DataFrame(rows)
# 校验：诊断 m5 应等于实际成交
df['chk'] = df['m5'] - df['actual']
assert (df['chk'].abs() <= 0.5).all(), df[df['chk'].abs() > 0.5][['sym', 'm5', 'actual']]

df = df.sort_values('actual', ascending=False).reset_index(drop=True)
df.to_csv('step52_funnel.csv', index=False, float_format='%.4g')

pd.set_option('display.width', 240, 'display.max_columns', 30)
print(f"全市场 {len(df)} 个品种；诊断 m5 总和 {int(df.m5.sum())}，实际成交总和 {int(df.actual.sum())}")
print("\n=== 全市场汇总漏斗（笔数）===")
tot = df[['flip', 'raw', 'p2', 'blk', 'm1a', 'm5']].sum()
print(tot.to_string())
print(f"\n方向翻转 → raw(过P6/ADX/P4/支路): {tot.raw/tot.flip*100:.1f}% 存活")
print(f"raw → P2高波日: {(tot.p2/tot.raw*100):.1f}% 存活")
print(f"P2 → 簇冲击日:  {(tot.blk/tot.p2*100):.1f}% 存活")
print(f"簇 → M1a:      {(tot.m1a/tot.blk*100):.1f}% 存活")
print(f"M1a → M5会员门: {(tot.m5/tot.m1a*100):.1f}% 存活  ← 最大一刀")
print(f"\nM5 会员门 |z|>=1.0 全市场平均覆盖率: {df.mz_cov.mean()*100:.1f}%")

print("\n=== 用户6候选 vs 活跃品种的漏斗对比 ===")
focus = ['MA888', 'EG888', 'EB888', 'BR888', 'SA888', 'SI888']
active = ['AU888', 'J888', 'SC888', 'P888', 'I888', 'M888', 'RB888']
def show(names, title):
    print(f"\n--- {title} ---")
    sub = df[df.sym.isin(names)][['sym', 'flip', 'raw', 'p2', 'blk', 'm1a', 'm5', 'mz_cov', 'actual']]
    print(sub.to_string(index=False, float_format=lambda x: f'{x:.3g}'))
show(focus, '用户6候选（稀疏组）')
show(active, '活跃品种（信号密集组）')

print("\n=== 成交最少 / 最多各8个 ===")
print("最少:", df.tail(8)[['sym', 'm5', 'actual', 'mz_cov']].to_string(index=False, float_format=lambda x: f'{x:.3g}'))
print("最多:", df.head(8)[['sym', 'm5', 'actual', 'mz_cov']].to_string(index=False, float_format=lambda x: f'{x:.3g}'))

# 相关性：mz_cov 与成交数的关系
corr = df[['mz_cov', 'actual']].corr().iloc[0, 1]
print(f"\n会员门覆盖率 mz_cov 与 成交数 的相关系数: {corr:.3f}")
