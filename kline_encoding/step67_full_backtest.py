# -*- coding: utf-8 -*-
"""全样本 V5X 回测：在【校正后的同花顺连续60m网格】上，与【原整数整点桶网格 bar_60m_ba】
做同口径 A/B。两网格覆盖同一 ~57 品种全集（2020-2026-06），唯一变量是『网格口径』。

同时：
  - 在『原始 42 品种』(results_step46.json 的 per_sym) 上做 THS vs BA 的直接 A/B，
    对应原全样本 +1,340,409 的结论能否在正确网格上复现。
"""
import json, os
import numpy as np
import pandas as pd
import step46_prod_bt as s46
import step61_rerun_ths as S

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ---- 引擎公共依赖 (step61 的 run_grid 需要这些全局量) ----
ba_ba, raw_ba, dd, var, cost, fac = s46.load_all()
S.costmap = S.build_cost_fn(cost, var)
S.tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
S.fac = fac

MIN_BARS = 1500
EXCLUDE = {'CJ'}
ths_df, ths_raw = S.load_ths()

def universe(srccols, builder, ba, raw):
    """取有成本、>=MIN_BARS、非 excluded 的品种集合。"""
    out = []
    for s in srccols:
        v = s[:-3]
        if v in S.costmap and v not in EXCLUDE and (ba[ba.symbol == s].shape[0] >= MIN_BARS):
            out.append(s)
    return sorted(out)

ths_syms = universe(ths_df.symbol.unique(), S.build_sym_ths, ths_df, ths_raw)
ba_syms = universe(ba_ba.symbol.unique(), s46.build_sym, ba_ba, raw_ba)
like = sorted(set(ths_syms) & set(ba_syms))
print(f'THS universe({len(ths_syms)}): {ths_syms}')
print(f'BA  universe({len(ba_syms)}): {ba_syms}')
print(f'like-for-like({len(like)})')

# 原始 42 品种 (results_step46.json per_sym)
legacy = json.load(open('results_step46.json', encoding='utf-8'))
orig42 = sorted(set(legacy['V5X']['per_sym'].keys()))
orig42 = [s for s in orig42 if s in set(ths_syms) and s in set(ba_syms)]
print(f'orig42 (THS&BA 都在) = {len(orig42)}: {orig42}')

def run_on(syms, builder, ba, raw, label):
    shock = S.cluster_shock_days({s: builder(s, ba, raw, dd, var) for s in syms})
    m, t = S.run_grid(syms, builder, ba, raw, dd, var, shock, label)
    return m, t

results = {}

# 1) like-for-like A/B
print('\n===== (1) like-for-like: THS vs BA 同全集 %d 品种 =====' % len(like))
m_ths, t_ths = run_on(like, S.build_sym_ths, ths_df, ths_raw, 'THS_LIKE')
m_ba, t_ba = run_on(like, s46.build_sym, ba_ba, raw_ba, 'BA_LIKE')
results['like'] = {'syms': like, 'THS': m_ths, 'BA': m_ba}

# 2) 原始 42 品种直接 A/B
print('\n===== (2) orig42: THS vs BA 同 %d 品种 (对应原 +1.34M) =====' % len(orig42))
m_o_ths, _ = run_on(orig42, S.build_sym_ths, ths_df, ths_raw, 'THS_ORIG42')
m_o_ba, _ = run_on(orig42, s46.build_sym, ba_ba, raw_ba, 'BA_ORIG42')
results['orig42'] = {'syms': orig42, 'THS': m_o_ths, 'BA': m_o_ba}

# 3) THS 全集（自身完整覆盖）
print('\n===== (3) THS 全宇宙 %d 品种 =====' % len(ths_syms))
m_tf, _ = run_on(ths_syms, S.build_sym_ths, ths_df, ths_raw, 'THS_FULL')
results['ths_full'] = {'syms': ths_syms, 'THS': m_tf}

# 汇总打印
def show(tag, d):
    v = d['V5X']
    ho = v.get('holdout', {}); tr = v.get('train', {})
    print(f'  [{tag}] V5X: n={v["n"]} net={v["net_yuan"]:,.0f} pf={v["pf"]:.3f} '
          f'win={v["win"]:.3f} mdd={v["mdd_yuan"]:,.0f}')
    print(f'        train net={tr.get("net_yuan"):,.0f} | holdout(2026) net={ho.get("net_yuan"):,.0f} pf={ho.get("pf")} n={ho.get("n")}')

print('\n================ A/B 摘要 ================')
print('-- like-for-like (全集) --'); show('THS', m_ths); show('BA', m_ba)
print('-- orig42 (原 +1.34M 对应集合) --'); show('THS', m_o_ths); show('BA', m_o_ba)
print(f'\n[参考] 原 step46 全样本 V5X: net={legacy["V5X"]["net_yuan"]:,.0f} pf={legacy["V5X"]["pf"]:.3f} n={legacy["V5X"]["n"]}')

json.dump(results, open('results_ths_full.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
print('\nsaved results_ths_full.json')
