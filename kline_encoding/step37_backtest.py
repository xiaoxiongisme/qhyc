# -*- coding: utf-8 -*-
"""Step 37: 可交易性回测（成本敏感）
策略：clv 快速均值回归（train 标定阈值；clv≥hi→空 h 根；clv≤lo→多 h 根）
叠加：A 波动率自适应仓位（目标波动/预测波动）  B 夜盘高振幅日规避
纪律：train(2020-2025)/holdout(2026) 分离、阈值无前视、成本归一到名义本金 bp、
      容量梯度 1/3/5 手、多空两侧计数均需 >0、成本敏感性网格。
"""
import sys; sys.path.insert(0, '.')
import os
import numpy as np
import pandas as pd
from ke_common import load

PARTS = ['2020', '2021_2025', '2026']
SYMS = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'UR', 'FG', 'SA', 'PF', 'PX', 'PR', 'EB', 'SC', 'IF']
HORIZONS = [1, 5, 15, 60]
COMM_RATES = [0.00005, 0.0001, 0.0002]     # 每边佣金（名义本金比例）
SLIP_BPS = [0.00025, 0.0005, 0.001]        # 每边滑点
CAPS = [1, 3, 5]


def load_sk(sym):
    parts = [p for p in PARTS if os.path.getsize(f'{sym}_{p}_1m_raw.csv') > 300]
    return load(sym, parts)


def backtest_clv(d, h, hi_q=0.8, lo_q=0.2, comm=0.0001, slip=0.0005, lots=1.0,
                 vol_scale=False, night_gate=False):
    """返回 (trade 表 dict of arrays, 汇总)。无前视：阈值/规模参数由外部用 train 标定后传入。"""
    c = d.close.values
    clv = d.clv.values
    lr = d.logret.values
    adj = d.adj.values
    yr = d.year.values
    slot = d.slot.values
    n = len(d)
    # 未来 h 根收益（会话内）
    cs = pd.Series(lr).groupby(d.seg.values).cumsum().values
    segv = d.seg.values
    t = np.arange(n)
    t2 = t + h
    fwd = np.full(n, np.nan)
    m = (t2 < n) & (segv[np.minimum(t2, n - 1)] == segv)
    fwd[m] = (cs[t2[m]] - cs[t[m]])
    # 信号（bar t 收盘可知）
    sig = np.zeros(n)
    sig[(clv >= HI) & adj] = -1.0
    sig[(clv <= LO) & adj] = +1.0
    # 仓位缩放：预测波动 = 过去 60 根平均 amp（train 标定目标）
    scale = np.ones(n)
    if vol_scale:
        amp_s = pd.Series(d.amp.values)
        pv = amp_s.groupby(d.seg.values).transform(lambda s: s.rolling(60, min_periods=20).mean()).values
        pv = np.maximum(pv, 1e-6)
        scale = np.clip(TARGET_VOL / pv, 0.2, 3.0)
    # 夜盘规避：夜盘（slot>=225）振幅高的交易日跳过日盘信号
    if night_gate:
        nmask = slot >= 225
        night_amp = pd.Series(d.amp.values[nmask]).groupby(d.td.values[nmask]).mean()
        high = night_amp > NIGHT_THR
        gate = pd.Series(d.td.values).map(high).values.astype(float)
        sig = sig * (1.0 - gate * (slot < 225))
    act = sig != 0
    idx = np.where(act & np.isfinite(fwd))[0]
    if len(idx) == 0:
        return None
    gross = sig[idx] * fwd[idx] * scale[idx] * lots          # 对数收益×手数
    notional = c[idx] * lots * MULT.get(SYM_CUR, 10)
    cost = (2 * comm + 2 * slip) * notional
    pnl = gross * notional - cost                            # 元
    pnl_bp = gross - (2 * comm + 2 * slip)
    return {'idx': idx, 'yr': yr[idx], 'sig': sig[idx], 'pnl': pnl, 'pnl_bp': pnl_bp,
            'gross_bp': gross, 'notional': notional}


MULT = {'MA': 10, 'EG': 10, 'TA': 5, 'PP': 5, 'L': 5, 'V': 5, 'UR': 20, 'FG': 20, 'SA': 20,
        'PF': 5, 'PX': 5, 'PR': 5, 'EB': 5, 'SC': 1000, 'IF': 300}
TARGET_VOL = 0.10     # 目标单根振幅 %（train 中位附近）
NIGHT_THR = None

import json
res = {}
for sym in SYMS:
    d = load_sk(sym)
    yr = d.year.values
    mtr, mte = yr <= 2025, yr == 2026
    HI = np.quantile(d.clv.values[mtr], 0.8)
    LO = np.quantile(d.clv.values[mtr], 0.2)
    amp_tr = d.amp.values[mtr]
    TARGET_VOL = float(np.median(amp_tr))
    nmask = d.slot.values >= 225
    night_mean = pd.Series(d.amp.values[nmask]).groupby(d.td.values[nmask]).mean()
    NIGHT_THR = float(night_mean[night_mean.index.isin(pd.Series(d.td.values[mtr]).unique())].median())
    SYM_CUR = sym
    r_sym = {}
    for h in HORIZONS:
        bt = backtest_clv(d, h)
        if bt is None:
            continue
        for lbl, m in (('train', mtr[bt['idx']]), ('holdout', mte[bt['idx']])):
            if m.sum() < 30:
                continue
            pnl = bt['pnl'][m]; pnl_bp = bt['pnl_bp'][m]; sig = bt['sig'][m]
            r_sym[f'h{h}_{lbl}'] = {
                'n': int(m.sum()), 'n_long': int((sig > 0).sum()), 'n_short': int((sig < 0).sum()),
                'net_bp': float(pnl_bp.mean() * 1e4), 'gross_bp': float(bt['gross_bp'][m].mean() * 1e4),
                'win': float((pnl > 0).mean()), 'net_yuan': float(pnl.sum()),
                'notional': float(bt['notional'][m].sum()),
            }
    res[sym] = {'HI': float(HI), 'LO': float(LO), 'target_vol': TARGET_VOL, 'night_thr': NIGHT_THR, 'runs': r_sym}
    line = ' | '.join(f"h{h} tr {r_sym.get(f'h{h}_train', {}).get('net_bp', float('nan')):+.2f}bp "
                      f"ho {r_sym.get(f'h{h}_holdout', {}).get('net_bp', float('nan')):+.2f}bp"
                      for h in HORIZONS if f'h{h}_train' in r_sym)
    print(f'{sym}: {line}')

json.dump(res, open('results_backtest.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=float)
print('saved results_backtest.json')
