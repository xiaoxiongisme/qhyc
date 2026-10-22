# -*- coding: utf-8 -*-
"""step77 THS 跨网格复核 + M5 方向过滤替换比对（请求#1、#3）
- 推荐参数：SL3/TR2/BE1/CD1/AL2/AT1.5（来自 step73/74 扫描）
- 网格：BA（9棒/日，后复权） vs THS（6棒/日，后复权，2020+）
- 门控：V5X 现用 M5(|z|>=1.0 幅度) vs M5b(|z|>=1.0 且 sign(z)==方向)
- 输出：step77_validation.json（含 per_sym）
"""
import json, time
import numpy as np
import pandas as pd
import step46_prod_bt as S

DATA = 'prod50'
OUT = 'step77_validation.json'

# ---- 推荐参数（补丁 step46 全局量，不改引擎逻辑）----
S.SL_ATR, S.TRAIL_ATR, S.BE_R = 3.0, 2.0, 1.0
S.COOLDOWN, S.ADD_MAX_LOTS, S.ADD_THR = 1, 2, 1.5

t0 = time.time()
ba = pd.read_csv(f'{DATA}/bar_60m_ba.csv.gz', parse_dates=['ts'])
raw = pd.read_csv(f'{DATA}/bar_60m_raw.csv.gz', parse_dates=['ts'])
ths = pd.read_csv(f'{DATA}/bar_60m_ths.csv.gz', parse_dates=['ts'])
dd = pd.read_csv(f'{DATA}/daily_bar.csv.gz', parse_dates=['trade_date'])
var = pd.read_csv(f'{DATA}/dim_variety.csv')
cost = pd.read_csv(f'{DATA}/dim_trading_cost.csv')
fac = pd.read_csv(f'{DATA}/factor_value.csv.gz', parse_dates=['trade_date', 'available_at'])

costmap = S.build_cost_fn(cost, var)
tick = dict(zip(var.variety_code, var.tick_size.astype(float)))

def build_grid(ba_g, raw_g):
    data = {}
    syms = sorted(s for s in ba_g.symbol.unique()
                  if s[:-3] in costmap and s[:-3] not in S.EXCLUDE)
    for s in syms:
        try:
            b = S.build_sym(s, ba_g, raw_g, dd, var)
            if b is not None:
                data[s] = b
        except Exception as e:
            print(f'{s}: SKIP {type(e).__name__} {e}')
    data = S.attach_member_z(data, fac)
    shock = S.cluster_shock_days(data)
    return data, shock, syms

data_ba, shock_ba, syms_ba = build_grid(ba, raw)
# THS：作为 ba 与 raw 同源（后复权连续网格；成本按后复权价计，绝对值与 BA 不可直接比，但相对指标可比）
data_ths, shock_ths, syms_ths = build_grid(ths, ths)
print(f'[load] BA syms={len(data_ba)} THS syms={len(data_ths)} ({time.time()-t0:.1f}s)', flush=True)

def run_grid(data, shock, flags, scope=S.P4_SECTORS):
    tb = {}
    for sym in data:
        vc = sym[:-3]
        cfg = {'cost': costmap[vc], 'tick': tick.get(vc, 1.0),
               'blocked_tds': shock.get(S.CLUSTER.get(vc), set())}
        if S.CLUSTER.get(vc) in scope:
            cfg['entry_mode'] = 'pullback'
        tb[sym] = S.run_symbol(sym, data[sym], cfg, flags)
    trades = [t for ts in tb.values() for t in ts]
    m = S.metrics(trades, '+'.join(sorted(flags)))
    return m, trades

grids = {'BA': (data_ba, shock_ba), 'THS': (data_ths, shock_ths)}
flagdefs = {
    'M5':  {'A1S', 'B3', 'M1a_pb', 'M5'},
    'M5b': {'A1S', 'B3', 'M1a_pb', 'M5b'},
}
results = {}
for gname, (data, shock) in grids.items():
    results[gname] = {}
    for fname, flags in flagdefs.items():
        m, trades = run_grid(data, shock, flags)
        per_sym = {s: S.metrics([t for t in trades if t['sym'] == s], s)
                   for s in sorted({t['sym'] for t in trades})}
        results[gname][fname] = {'metrics': m, 'per_sym': per_sym}
        print(f'[{gname}/{fname}] n={m["n"]} net={m["net_yuan"]:.0f} pf={m["pf"]:.3f} '
              f'mdd={m["mdd_yuan"]:.0f} mar={m["mar"]:.2f} | '
              f'train_net={m["train"]["net_yuan"]:.0f} ho_net={m["holdout"]["net_yuan"]:.0f}', flush=True)

json.dump(results, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
print(f'[done] -> {OUT} ({time.time()-t0:.1f}s)')
