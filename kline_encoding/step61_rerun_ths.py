# -*- coding: utf-8 -*-
"""step61 — 用【同花顺连续60分钟网格】(prod50/bar_60m_ths.csv.gz) 重跑融合 V5X 回测

背景：
  原生产回测 step46 用的是云端整数整点桶 60m(bar_60m_ba, 9根/日)，与同花顺实盘
  网格(连续60交易分钟累加, 化工/黑色6根/日)结构错位 —— 这是「信号对不上图」的根因。
  现已把数据库全部品种重建为同花顺网格入库(prod50/bar_60m_ths.csv.gz)，本步在其上
  重跑 V5X，并与「原整数桶网格」在【同一14金标准品种】上做 A/B，隔离「网格口径」影响。

关键修正 (相对 step46.build_sym)：
  sess_last 不再用 hour==14 / night_tail-1 这类整点硬编码，而是用「下一根时间间隔
  >= 360 分钟」判定会话末根(日盘收盘/夜盘收盘)，午餐(化工180min)等日内休息不误判。
  其余(td_assign / hvol / 指标 / 门控)完全复用 step46，保证唯一变量是「网格口径」。

覆盖说明：
  THS 数据里 14 个金标准品种有 2020+ 全历史(>=8000根)，其余 46 品种仅有 Sina 近窗口
  (~1023根, 多为2026年)，低于 build_sym 的 1500 根阈值 → 主回测仅含金标准14品种
  (与原 ba 网格回测在「同一品种集」上比较)。46 品种的全历史需从 1m 原始重建，未含本次。
"""
import json, sys, os
import numpy as np
import pandas as pd
import step46_prod_bt as s46

os.chdir(os.path.dirname(os.path.abspath(__file__)))   # 锚定工作目录, 保证 step46 相对路径可解析

THS = 'prod50/bar_60m_ths.csv.gz'
DATA = 'prod50'
EXCLUDE = {'CJ'}                       # 红枣日线复权损坏(原口径一致)
GOLD = {'EB', 'EG', 'FG', 'IF', 'L', 'MA', 'PF', 'PP', 'PR', 'PX', 'SA', 'TA', 'UR', 'V'}
MIN_BARS = 1500                        # 与 step46.build_sym 一致

# 复用的引擎与辅助
run_symbol = s46.run_symbol
apply_a2 = s46.apply_a2
metrics = s46.metrics
build_cost_fn = s46.build_cost_fn
td_assign = s46.td_assign
attach_member_z = s46.attach_member_z
cluster_shock_days = s46.cluster_shock_days
macd_hist = s46.macd_hist
kdj = s46.kdj
CLUSTER = s46.CLUSTER
P4_SECTORS = s46.P4_SECTORS
WARMUP_DAYS = s46.WARMUP_DAYS


# ---------------------------------------------------------------- THS 数据装载
def load_ths():
    ba = pd.read_csv(THS, parse_dates=['ts'])
    raw = ba.copy()                    # THS 单序列, 复权刻度≈bar_60m_ba; 成本口径用同一序列
    return ba, raw


def build_sym_ths(sym, ba, raw, dd, var):
    """同花顺连续60m网格版 build_sym：sess_last 用间隔>=360min 判定会话末根。"""
    b = ba[ba.symbol == sym].sort_values('ts').reset_index(drop=True)
    if len(b) < MIN_BARS:
        return None
    r = raw[raw.symbol == sym].sort_values('ts').reset_index(drop=True)
    b['raw_o'] = r.open.values
    b['raw_c'] = r.close.values
    dly = dd[dd.symbol == sym].sort_values('trade_date').reset_index(drop=True)
    cal = dly.trade_date
    b['td'] = td_assign(b.ts, cal)
    b['hour'] = b.ts.dt.hour
    b['year'] = b.ts.dt.year
    # ---- 会话首末根：间隔 >=360min 即跨会话(日盘收盘→夜盘 / 夜盘→次日 / 周末) ----
    gaps = b.ts.diff().dt.total_seconds() / 60.0          # gaps[k]=ts[k]-ts[k-1]
    nextgap = gaps.shift(-1)                              # 根 i 到 i+1 的间隔
    sess_last = ((nextgap >= 360) | b.index.to_series().eq(len(b) - 1)).fillna(False).values
    sess_first = (gaps >= 360).fillna(True).values
    b['sess_first'] = sess_first
    b['sess_last'] = sess_last
    # ---- 日线体制 (hvol 高波日禁开仓) ----
    dly['pc'] = dly.close.shift(1)
    dly['amp'] = (dly.high - dly.low) / dly.pc
    for q in [70, 80]:
        dly[f'q{q}'] = dly.amp.rolling(250, min_periods=60).quantile(q / 100.0)
        dly[f'hvol{q}'] = (dly.amp > dly[f'q{q}']).shift(1).fillna(False)
    hv = dly.set_index('trade_date')
    b['hvol'] = b.td.map(hv.hvol80).fillna(False).values.astype(bool)
    b['hvol70'] = b.td.map(hv.hvol70).fillna(False).values.astype(bool)
    # 预热：上市 200 个自然日后才允许事件
    b['allow'] = (b.ts >= (b.ts.iloc[0] + pd.Timedelta(days=WARMUP_DAYS))).values
    # ---- 指标(复权价, 与 step46 一致) ----
    c = b.close.values.astype(float)
    h = b.high.values.astype(float)
    l = b.low.values.astype(float)
    b['mh'] = macd_hist(c)
    k, d2, j = kdj(h, l, c)
    b['kdj_k'] = k
    b['kdj_d'] = d2
    v = b.volume.values.astype(float)
    vmed = pd.Series(v).rolling(20, min_periods=5).median().shift(1).to_numpy()
    b['vol_ratio'] = np.where(vmed > 0, v / np.maximum(vmed, 1e-9), np.nan)
    return b


# ---------------------------------------------------------------- 变体定义(复用 step46 语义)
VARIANTS = ['BASE', 'P2', 'P4', 'P6', 'V4NP2', 'V4X', 'V5', 'V5X']

def variant_cfg(v):
    flags, scope = set(), None
    if v == 'BASE':
        pass
    elif v == 'P2':
        flags = {'A1S'}
    elif v == 'P4':
        scope = P4_SECTORS
    elif v == 'P6':
        flags = {'B3'}
    elif v == 'V4NP2':
        flags = {'A1S', 'A2', 'B3'}; scope = P4_SECTORS
    elif v == 'V4X':
        flags = {'A1S', 'B3'}; scope = P4_SECTORS
    elif v == 'V5':
        flags = {'A1S', 'A2', 'B3', 'M1a_pb', 'M3', 'M5'}; scope = P4_SECTORS
    elif v == 'V5X':
        flags = {'A1S', 'B3', 'M1a_pb', 'M5'}; scope = P4_SECTORS
    else:
        raise ValueError(v)
    return flags, scope


def cfg_for(sym, entry_scope, blocked_tds):
    vc = sym[:-3]
    cl = CLUSTER.get(vc)
    cfg = {'cost': costmap[vc], 'tick': tick.get(vc, 1.0),
           'blocked_tds': blocked_tds.get(cl, set()) if cl else set()}
    if entry_scope and cl in entry_scope:
        cfg['entry_mode'] = 'pullback'
    return cfg


def run_grid(syms, builder, ba, raw, dd, var, shock, label):
    """对给定品种集跑全部变体；返回 {variant: metrics}, {variant: trades_df}"""
    data = {}
    for s in syms:
        try:
            b = builder(s, ba, raw, dd, var)
            if b is not None:
                data[s] = b
        except Exception as e:
            print(f'  {s}: SKIP {type(e).__name__} {e}', flush=True)
    data = attach_member_z(data, fac)
    out_m, out_t = {}, {}
    for v in VARIANTS:
        flags, scope = variant_cfg(v)
        tb = {sym: run_symbol(sym, data[sym], cfg_for(sym, scope, shock), flags - {'A2'})
              for sym in data}
        trades = apply_a2(tb) if 'A2' in flags else [t for ts in tb.values() for t in ts]
        m = metrics(trades, f'{label}/{v}')
        m['n_sym'] = len(data)
        out_m[v] = m
        out_t[v] = pd.DataFrame(trades)
        if v in ('V5X', 'V5', 'BASE'):
            out_t[v].to_csv(f'trades_ths_{label}_{v}.csv', index=False, encoding='utf-8-sig')
        print(f'  [{label}] {v}: n={m.get("n")} net={m.get("net_yuan"):,.0f} '
              f'pf={m.get("pf")} win={m.get("win"):.3f} mdd={m.get("mdd_yuan"):,.0f} '
              f'(holdout net={m.get("holdout",{}).get("net_yuan")})', flush=True)
    return out_m, out_t


# ---------------------------------------------------------------- 主流程
if __name__ == '__main__':
    # 公共：日线 / 品种 / 成本 / 因子 (两种网格共用)
    ba_ba, raw_ba, dd, var, cost, fac = s46.load_all()
    costmap = build_cost_fn(cost, var)
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    print('cost/tick loaded; factor rows =', len(fac), flush=True)

    # THS 网格
    ba_ths, raw_ths = load_ths()
    ths_syms = sorted(s for s in ba_ths.symbol.unique()
                      if s[:-3] in costmap and s[:-3] not in EXCLUDE
                      and (ba_ths[ba_ths.symbol == s].shape[0] >= MIN_BARS))
    print(f'THS universe (>= {MIN_BARS} bars, 有成本): {len(ths_syms)} -> {ths_syms}', flush=True)
    shock_ths = cluster_shock_days({s: build_sym_ths(s, ba_ths, raw_ths, dd, var)
                                   for s in ths_syms if ba_ths[ba_ths.symbol == s].shape[0] >= MIN_BARS})
    # 注意：上面构造仅用于 shock；实际 run_grid 内部会再 builder 一次(干净)
    m_ths, t_ths = run_grid(ths_syms, build_sym_ths, ba_ths, raw_ths, dd, var, shock_ths, 'THS')

    # 原整数桶网格(bar_60m_ba) —— 同一14金标准品种做控制
    ba_syms = sorted(s for s in ba_ba.symbol.unique()
                     if s[:-3] in costmap and s[:-3] not in EXCLUDE
                     and (ba_ba[ba_ba.symbol == s].shape[0] >= MIN_BARS))
    gold_ba = [s for s in ba_syms if s[:-3] in GOLD]
    print(f'BA universe total {len(ba_syms)}; gold-14 subset -> {gold_ba}', flush=True)
    shock_ba = cluster_shock_days({s: s46.build_sym(s, ba_ba, raw_ba, dd, var) for s in gold_ba})
    m_ba, t_ba = run_grid(gold_ba, s46.build_sym, ba_ba, raw_ba, dd, var, shock_ba, 'BA')

    # 汇总
    summary = {'THS': m_ths, 'BA': m_ba, 'gold_syms': gold_ba, 'ths_syms': ths_syms,
               'ths_coverage': {s: [str(ba_ths[ba_ths.symbol == s].ts.min()),
                                    str(ba_ths[ba_ths.symbol == s].ts.max()),
                                    int(ba_ths[ba_ths.symbol == s].shape[0])] for s in ths_syms}}
    json.dump(summary, open('results_ths.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('saved results_ths.json')

    # 打印核心 A/B
    print('\n===== V5X A/B (gold-14, 同一品种集) =====')
    for k in ['THS', 'BA']:
        mm = (m_ths if k == 'THS' else m_ba)['V5X']
        ho = mm.get('holdout', {})
        tr = mm.get('train', {})
        print(f'  [{k}] V5X: n={mm["n"]} net={mm["net_yuan"]:,.0f} pf={mm["pf"]:.3f} '
              f'win={mm["win"]:.3f} mdd={mm["mdd_yuan"]:,.0f}')
        print(f'        train net={tr.get("net_yuan"):,.0f} pf={tr.get("pf")} | '
              f'holdout(2026) net={ho.get("net_yuan"):,.0f} pf={ho.get("pf")} n={ho.get("n")}')
