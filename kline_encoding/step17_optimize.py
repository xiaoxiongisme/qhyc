# -*- coding: utf-8 -*-
"""
step17: 编码优化方案的数学验证与统计对比
验证链:
  V1 (open, chg%, amp%, CLV) <-> (O,H,L,C) 双射 + round-trip 100%
  V2 由 (chg, amp, CLV) 完全推导上下影线有无 (混淆矩阵)
  V3 I(CLV ; 原6位码) 互信息 —— CLV 吸收影线字段的证据
  V4 单字节量化编码 (实体2b + CLV3b + 振幅2b) vs 原6位码: R²/MI 对比
  V5 open 链式冗余度: P(open_t == close_{t-1})
  V6 三值实体(跌/平/涨) 后的方向概率修正
  V7 量化误差 vs 比特数曲线
"""
import pandas as pd, numpy as np, json, itertools
from scipy import stats

OUT = "."
STATES = ['000000','000001','000100','000101','010000','010001','010100','010101']

def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, '01', '00')
    up = np.where(h > np.maximum(o, c), '01', '00')
    lo = np.where(l < np.minimum(o, c), '01', '00')
    return np.char.add(np.char.add(body, up), lo)

def mi_bits(x, y, nx, ny):
    """x,y 为整数编码数组; 返回互信息(比特)"""
    N = len(x)
    H = np.zeros((nx, ny))
    np.add.at(H, (x, y), 1)
    P = H / N
    px = P.sum(1, keepdims=True); py = P.sum(0, keepdims=True)
    nz = P > 0
    return float((P[nz] * np.log2(P[nz] / (px @ py)[nz])).sum())

def r2_of(y, groups):
    grand = y.mean(); sst = ((y - grand) ** 2).sum(); ssr = 0.0
    for g in groups:
        if g.sum() < 10: continue
        ssr += ((y[g] - y[g].mean()) ** 2).sum()
    return float(1 - ssr / sst)

def t_of(x):
    x = np.asarray(x); x = x[~np.isnan(x)]
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0

RES = {}
for sym in ['MA', 'EG']:
    d0 = pd.read_csv(f'{sym}_2020_1m_raw.csv', parse_dates=['datetime'])
    d1 = pd.read_csv(f'{sym}_2021_2025_1m_raw.csv', parse_dates=['datetime'])
    d = pd.concat([d0, d1], ignore_index=True).sort_values('datetime').reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    n = len(d)
    chg = (c - o) / o * 100.0
    rng = h - l
    amp = rng / o * 100.0
    clv = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1.0), 0.5)
    code = encode_ohlc(o, h, l, c)
    gap = d.datetime.diff().dt.total_seconds().values
    adj = np.zeros(n, bool); adj[1:] = gap[1:] == 60.0
    R = {}

    # ---------- V1 双射 + round-trip ----------
    # 重建: low = c - CLV*range ; high = low + range
    rng_r = amp / 100.0 * o
    lo_r = c - clv * rng_r
    hi_r = lo_r + rng_r
    err_o = np.abs(hi_r - h); err_l = np.abs(lo_r - l)
    max_err = float(max(err_o.max(), err_l.max()))
    R['bijection'] = {
        'max_abs_err_price': max_err,
        'roundtrip_exact': bool(max_err < 1e-6),
        'n_bars': int(n)}

    # ---------- V2 由 (chg,amp,CLV) 推导影线有无 ----------
    # beta = body/range (有符号); upper/range = 1-CLV+min(beta,0); lower/range = CLV-max(beta,0)
    beta = chg / np.where(amp > 0, amp, np.nan)
    up_pred = (1 - clv) > np.maximum(-beta, 0) + 1e-12
    lo_pred = clv > np.maximum(beta, 0) + 1e-12
    up_true = h > np.maximum(o, c)
    lo_true = l < np.minimum(o, c)
    m_rng = amp > 0
    R['shadow_derivation'] = {
        'upper_acc': float((up_pred[m_rng] == up_true[m_rng]).mean()),
        'lower_acc': float((lo_pred[m_rng] == lo_true[m_rng]).mean()),
        'both_acc': float(((up_pred[m_rng] == up_true[m_rng]) & (lo_pred[m_rng] == lo_true[m_rng])).mean()),
        'n_range_gt0': int(m_rng.sum())}

    # ---------- V3 互信息: CLV vs 原码字段 ----------
    clv_i = np.clip((clv * 8).astype(int), 0, 7)          # 8档量化CLV
    body_i = np.where(c >= o, 1, 0)
    up_i = np.where(h > np.maximum(o, c), 1, 0)
    lo_i = np.where(l < np.minimum(o, c), 1, 0)
    code_i = np.array([int(x, 2) for x in code])         # 0..63 原样保留
    sel = adj[1:]
    sidx = {s: i for i, s in enumerate(STATES)}
    code_i = np.array([sidx[x] for x in code])          # 0..7
    pc = code_i[:-1][sel]; pclv = clv_i[:-1][sel]; pbody = body_i[:-1][sel]
    pup = up_i[:-1][sel]; plo = lo_i[:-1][sel]
    nc = code_i[1:][sel]
    R['mutual_info_bits'] = {
        'MI(CLV8; next_code8)': mi_bits(pclv, nc, 8, 8),
        'MI(body; next_code8)': mi_bits(pbody, nc, 2, 8),
        'MI(upper; next_code8)': mi_bits(pup, nc, 2, 8),
        'MI(lower; next_code8)': mi_bits(plo, nc, 2, 8),
        'MI(code8; next_code8)': mi_bits(pc, nc, 8, 8),
        'MI(CLV8; upper)': mi_bits(pclv, pup, 8, 2),
        'MI(CLV8; lower)': mi_bits(pclv, plo, 8, 2),
        'MI(CLV8; body)': mi_bits(pclv, pbody, 8, 2)}

    # ---------- V4 单字节量化编码 vs 原6位码 ----------
    # 新码: 实体2b(00跌/01平/10涨) + CLV3b(8档) + 振幅2b(4档) = 7b
    ampq = np.clip((amp / max(amp.max(), 1e-9) * 4).astype(int), 0, 3)  # 先按全局最大值粗分, 下面用分位
    qs = np.quantile(amp[m_rng], [0.25, 0.5, 0.75])
    ampq = np.digitize(amp, qs)  # 0..3
    body3 = np.where(c > o, 2, np.where(c == o, 1, 0))
    newcode = body3 * 32 + clv_i * 4 + ampq   # 0..255
    R['newcode_range'] = {'min': int(newcode.min()), 'max': int(newcode.max()),
                          'distinct': int(len(np.unique(newcode)))}
    yc = chg[1:][sel]; ya = amp[1:][sel]
    pnc_raw = newcode[:-1][sel]
    uniq = np.unique(pnc_raw)
    remap = {v: i for i, v in enumerate(uniq)}
    pnc = np.array([remap[v] for v in pnc_raw])
    R['predict_compare'] = {
        'R2_nextchg_old8': r2_of(yc, [pc == i for i in range(8)]),
        'R2_nextchg_new': r2_of(yc, [pnc == v for v in range(len(uniq))]),
        'R2_nextamp_old8': r2_of(ya, [pc == i for i in range(8)]),
        'R2_nextamp_new': r2_of(ya, [pnc == v for v in range(len(uniq))]),
        'MI_nextchg_old8': mi_bits(pc, np.digitize(yc, np.quantile(yc, [0.25, 0.5, 0.75])), 8, 4),
        'MI_nextchg_new': mi_bits(pnc, np.digitize(yc, np.quantile(yc, [0.25, 0.5, 0.75])), len(uniq), 4)}

    # ---------- V5 open 链式冗余 ----------
    cont = np.zeros(n, bool)
    cont[1:] = adj[1:] & (np.abs(o[1:] - c[:-1]) < 1e-9)
    jump_idx = np.where(adj[1:] & ~cont[1:])[0] + 1
    jump_hours = d.datetime.dt.hour.values[jump_idx]
    jump_minutes = d.datetime.dt.minute.values[jump_idx]
    hm = pd.Series([f'{h:02d}:{m:02d}' for h, m in zip(jump_hours, jump_minutes)]).value_counts()
    R['open_chain'] = {
        'p_open_eq_prev_close_within_adj': float(cont[1:][adj[1:]].mean()),
        'n_adjacent_pairs': int(adj[1:].sum()),
        'n_chain_ok': int(cont[1:].sum()),
        'n_jump': int(len(jump_idx)),
        'jump_time_top': {k: int(v) for k, v in hm.head(8).items()},
        'jump_mag_median_pct': (float(np.median(np.abs((o[jump_idx] - c[jump_idx - 1]) / c[jump_idx - 1] * 100))) if len(jump_idx) else 0.0)}

    # ---------- V8 熵对比: 原6位码 vs 新字节码 ----------
    def entropy_bits(x):
        v, cnt = np.unique(x, return_counts=True)
        p = cnt / cnt.sum()
        return float(-(p * np.log2(p)).sum())
    R['entropy'] = {
        'old_code8_states': entropy_bits(code_i[:-1][sel]),
        'old_code8_max': 3.0,
        'new_code_states': entropy_bits(pnc),
        'new_code_max_bits': float(np.log2(len(uniq))),
        'clv8_entropy': entropy_bits(pclv)}

    # ---------- V6 三值实体方向概率 ----------
    nxt_up = (chg[1:] > 0)[sel]; nxt_dn = (chg[1:] < 0)[sel]; nxt_dj = (chg[1:] == 0)[sel]
    prev = np.where(c[:-1] > o[:-1], 2, np.where(c[:-1] == o[:-1], 1, 0))[sel]
    R['ternary'] = {}
    for v, nm in [(0, 'prev_down'), (1, 'prev_flat'), (2, 'prev_up')]:
        m = prev == v
        if m.sum() > 30:
            R['ternary'][nm] = {
                'n': int(m.sum()),
                'p_next_up': float(nxt_up[m].mean()),
                'p_next_dn': float(nxt_dn[m].mean()),
                'p_next_flat': float(nxt_dj[m].mean()),
                'mean_next_chg': float(yc[m].mean()),
                't': t_of(yc[m])}

    # ---------- V7 量化误差 vs 比特 ----------
    # chg 均匀量化到 b 比特 (范围 +-1.5%, 覆盖 99.9%)
    def quant_err(x, b, lo, hi):
        K = 2 ** b
        step = (hi - lo) / K
        xq = np.clip(np.round((x - lo) / step), 0, K - 1) * step + lo + step / 2
        return float(np.abs(xq - x).mean()), float(np.abs(xq - x).max())
    c99 = np.quantile(np.abs(chg[m_rng]), 0.999)
    a99 = np.quantile(amp[m_rng], 0.999)
    rows = []
    for bc, ba in [(6, 4), (7, 5), (8, 5), (8, 6), (10, 7)]:
        ce, cm = quant_err(chg[m_rng], bc, -c99, c99)
        ae, am = quant_err(amp[m_rng], ba, 0, a99)
        rows.append({'bits_chg': bc, 'bits_amp': ba, 'mae_chg_pct': ce,
                     'mae_amp_pct': ae, 'max_chg_pct': cm})
    R['quant_bits'] = rows
    R['chg_abs_q999'] = float(c99); R['amp_q999'] = float(a99)
    RES[sym] = R
    print(f"===== {sym} (n={n:,}) =====")
    print(f"[V1] round-trip 最大绝对误差 = {max_err:.2e} 价格单位 -> {'无损双射 PASS' if max_err < 1e-6 else 'FAIL'}")
    s = R['shadow_derivation']
    print(f"[V2] 影线推导准确率: 上影={s['upper_acc']:.4f} 下影={s['lower_acc']:.4f} 两者皆对={s['both_acc']:.4f}")
    mi = R['mutual_info_bits']
    print(f"[V3] MI(比特): CLV8->下根码={mi['MI(CLV8; next_code8)']:.3f} | 上影={mi['MI(upper; next_code8)']:.3f} "
          f"下影={mi['MI(lower; next_code8)']:.3f} 实体={mi['MI(body; next_code8)']:.3f} | 原码8态={mi['MI(code8; next_code8)']:.3f}")
    print(f"     MI(CLV8;上影)={mi['MI(CLV8; upper)']:.3f} MI(CLV8;下影)={mi['MI(CLV8; lower)']:.3f} MI(CLV8;实体)={mi['MI(CLV8; body)']:.3f}")
    p = R['predict_compare']
    print(f"[V4] R2(next_chg): 原码8态={p['R2_nextchg_old8']:.4f} -> 新字节码={p['R2_nextchg_new']:.4f} | "
          f"R2(next_amp): {p['R2_nextamp_old8']:.4f} -> {p['R2_nextamp_new']:.4f} | 新码取值数={R['newcode_range']['distinct']}")
    oc = R['open_chain']
    print(f"[V5] 相邻分钟内 open_t == close_(t-1): {oc['n_chain_ok']:,}/{oc['n_adjacent_pairs']:,} = {oc['p_open_eq_prev_close_within_adj']*100:.2f}%; "
          f"断链 {oc['n_jump']:,} 处" + (f", 时刻TOP: {list(oc['jump_time_top'].items())[:4]}" if oc['n_jump'] else " (连续分钟内零断链)"))
    e = R['entropy']
    print(f"[V8] 熵: 原码={e['old_code8_states']:.3f}/{e['old_code8_max']:.0f}bit | CLV8={e['clv8_entropy']:.3f}bit | 新字节码={e['new_code_states']:.3f}bit ({len(uniq)}态)")
    print(f"[V6] 三值实体: " + " | ".join(
        f"{k}: n={v['n']:,} P(涨)={v['p_next_up']:.3f} P(跌)={v['p_next_dn']:.3f} P(平)={v['p_next_flat']:.3f} mean={v['mean_next_chg']:+.4f}%"
        for k, v in R['ternary'].items()))
    print(f"[V7] |chg|99.9分位={R['chg_abs_q999']:.3f}% amp99.9分位={R['amp_q999']:.3f}%")
    for r in rows:
        print(f"     chg {r['bits_chg']}b + amp {r['bits_amp']}b: 平均误差 chg {r['mae_chg_pct']*100:.2f}bp / amp {r['mae_amp_pct']*100:.2f}bp")

json.dump(RES, open(f'{OUT}/results_optimize.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print("\nsaved -> results_optimize.json")
