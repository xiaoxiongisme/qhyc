"""EB/EG 2025-2026 信号缺失根因验证：
1) 含 M5 vs 去 M5(ALT_GATE='none') 的年份漏斗对比 → 去 M5 后近两年是否有信号
2) EB/EG 2025-2026 会员净持仓 |z| 分布 → 确认是'有值<1.0'而非数据缺失
使用已验证引擎（DIAGNOSE 年份维度）。
"""
import numpy as np, pandas as pd
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CLUSTER, SymbolEngine, DailyCtx, CostCfg

ba, raw, dd, var, cost, fac = s46.load_all()
costmap = s46.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
EXCL = s46.EXCLUDE
syms = [s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in EXCL]
data = {s: b for s in syms if (b:=s46.build_sym(s,ba,raw,dd,var)) is not None}
data = s46.attach_member_z(data, fac)
shock = s46.cluster_shock_days(data)

def run(s, gate):
    vc = s[:-3]; fee_row, slip, mult = costmap[vc]
    cfg = V5XConfig(); cfg.DIAGNOSE = True; cfg.ADD_MAX_LOTS = 2
    cfg.ALT_GATE = gate
    eng = SymbolEngine(s, cfg, CostCfg(fee_row, slip, mult, tick.get(vc,1.0)))
    cl = CLUSTER.get(vc); blocked = shock.get(cl, set())
    sl_hours = frozenset(int(x) for x in data[s].loc[data[s].sess_last, 'hour'].unique())
    prev_td=[None]
    for row in data[s].itertuples():
        td = row.td
        if td != prev_td[0]:
            eng.set_daily_ctx(DailyCtx(td=td, hvol=bool(row.hvol),
                member_z=float(row.member_z) if not pd.isna(row.member_z) else np.nan,
                blocked=td in blocked, sess_last_hours=sl_hours))
            prev_td[0]=td
        eng.on_bar(row.ts, float(row.open),float(row.high),float(row.low),float(row.close),
                   float(row.volume), float(row.raw_c), allow=bool(row.allow))
    return eng._stat_y

print("="*70)
print("EB/EG 年份漏斗：含 M5(ALT_GATE='m5') vs 去 M5(ALT_GATE='none')")
print("="*70)
for s in ['EB888','EG888']:
    for gate in ['m5','none']:
        sy = run(s, gate)
        print(f"\n{s}  [{gate}]：")
        for y in sorted(sy.keys()):
            if y in (2025,2026):
                st = sy[y]
                print(f"  {y}: raw={st['raw']:3d} p2={st['p2']:3d} m1a={st['m1a']:3d} m5/成交={st['m5']:3d}")

print("\n" + "="*70)
print("EB/EG 2025-2026 会员净持仓 |z| 分布（确认缺失还是真<1.0）")
print("="*70)
for s in ['EB888','EG888']:
    b = data[s]; ts = pd.to_datetime(b['ts'])
    mz = b['member_z'].copy()
    mz.index = ts
    for yr in (2025, 2026):
        mzy = mz[mz.index.year==yr].dropna()
        n = len(mzy)
        if n == 0:
            print(f"{s} {yr}: 会员数据全缺失（NaN）— 若 M5 对 NaN 放行则不应被砍")
            continue
        cov = (mzy.abs() >= 1.0).mean()
        nonzero = (mzy.abs() >= 0.5).mean()
        print(f"{s} {yr}: 有效会员日 {n} 天, |z|>=1.0 覆盖 {cov*100:4.1f}%, "
              f"|z|>=0.5 覆盖 {nonzero*100:4.1f}%, 中位|z| {mzy.abs().median():.2f}, 最大|z| {mzy.abs().max():.2f}")
