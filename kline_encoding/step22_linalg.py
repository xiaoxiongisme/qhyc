# -*- coding: utf-8 -*-
"""线性代数视角：协方差谱、滞后协方差（可预测性）、格拉姆矩阵、范数、投影回归、共线性诊断。"""
import pandas as pd, numpy as np, json

OUT = '.'
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
SIDX = {s: i for i, s in enumerate(STATES)}
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
FEATS = ['涨跌幅', '振幅', 'CLV', '上影', '下影']


def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, '01', '00')
    up = np.where(h > np.maximum(o, c), '01', '00')
    lo = np.where(l < np.minimum(o, c), '01', '00')
    return np.char.add(np.char.add(body, up), lo)


def load(sym):
    d0 = pd.read_csv(f'{sym}_2020_1m_raw.csv', parse_dates=['datetime'])
    d1 = pd.read_csv(f'{sym}_2021_2025_1m_raw.csv', parse_dates=['datetime'])
    d = pd.concat([d0, d1], ignore_index=True).sort_values('datetime').reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    rng = h - l
    safe = np.where(rng > 0, rng, 1.0)
    d['state'] = np.array([SIDX[x] for x in encode_ohlc(o, h, l, c)])
    d['chg'] = (c - o) / o * 100.0
    d['amp'] = rng / o * 100.0
    d['clv'] = np.where(rng > 0, (c - l) / safe, 0.5)
    d['up_sh'] = np.where(rng > 0, (h - np.maximum(o, c)) / safe, 0.0)
    d['lo_sh'] = np.where(rng > 0, (np.minimum(o, c) - l) / safe, 0.0)
    gap = d.datetime.diff().dt.total_seconds().values
    d['adj'] = np.concatenate([[False], gap[1:] == 60.0])
    return d


RES = {}
for sym in ['MA', 'EG']:
    d = load(sym)
    n = len(d)
    W = np.column_stack([d.chg.values, d.amp.values, d.clv.values, d.up_sh.values, d.lo_sh.values])
    st = d.state.values
    adj = d.adj.values
    Z = (W - W.mean(0)) / W.std(0, ddof=1)
    R = {'sym': sym, 'n': int(n)}

    # ---------- ① 协方差矩阵 Σ 的谱 ----------
    S = np.cov(Z.T)
    lam, V = np.linalg.eigh(S)
    lam = lam[::-1]; V = V[:, ::-1]
    cond = float(lam[0] / lam[-1])
    p = lam / lam.sum()
    sent = float(-(p[p > 0] * np.log2(p[p > 0])).sum())          # 谱熵(bit)
    pr = float((lam.sum() ** 2) / (lam ** 2).sum())               # 参与率=有效维度
    R['Sigma'] = {'matrix': S.tolist(), 'eig': [float(x) for x in lam], 'cond': cond,
                  'spec_entropy_bits': sent, 'participation_ratio': pr,
                  'eigvec': V.tolist()}

    # ---------- ② 滞后协方差 Γ1 与可预测性 ----------
    A = Z[:-1][adj[1:]]          # 当前
    B = Z[1:][adj[1:]]           # 下一根
    Gamma1 = A.T @ B / len(A)    # 5x5 非对称 (Cov(z_t, z_{t+1}))
    Sinv = np.linalg.inv(S)
    M = Gamma1.T @ Sinv @ Gamma1                    # PSD
    rho2 = np.sort(np.linalg.eigvalsh(Sinv @ M))[::-1]   # 平方典型相关 ∈[0,1]
    r2_coord = np.diag(M) / np.diag(S)              # 逐坐标线性可预测R²
    # 每个PC方向的可预测性 (v'Γ1 v / λ)
    pred_pc = []
    for i in range(5):
        v = V[:, i]
        pred_pc.append(float((v @ Gamma1 @ v) / lam[i]))
    R['Gamma1'] = {'matrix': Gamma1.tolist(),
                   'canonical_r2': [float(x) for x in rho2],
                   'avg_r2': float(rho2.mean()),
                   'r2_coord': [float(x) for x in r2_coord],
                   'pred_per_pc': pred_pc}

    # ---------- ③ 范数视角: 波动率聚集 = 范数自相关 ----------
    nrm = np.linalg.norm(Z, axis=1)
    amp_n = Z[:, 1]
    R['norm'] = {
        'acf1_norm': float(pd.Series(nrm).autocorr(1)),
        'acf1_amp_z': float(pd.Series(amp_n).autocorr(1)),
        'mean_norm': float(nrm.mean()),
        'p99_norm': float(np.quantile(nrm, 0.99))}

    # ---------- ④ 编码捕获率: one-hot 8态 vs PCA-3 vs 无损 ----------
    # 八态分组在5维标准化空间捕获的方差份额(逐维R²平均)
    r2_dims = []
    for j in range(5):
        y = Z[:, j]
        grand = y.mean(); sst = ((y - grand) ** 2).sum(); ssr = 0.0
        for i in range(8):
            m = st == i
            ssr += ((y[m] - y[m].mean()) ** 2).sum()
        r2_dims.append(float(1 - ssr / sst))
    R['capture'] = {'r2_code8_per_dim': r2_dims, 'r2_code8_mean': float(np.mean(r2_dims)),
                    'r2_pca3': float(lam[:3].sum() / lam.sum()),
                    'r2_pca2': float(lam[:2].sum() / lam.sum())}

    # ---------- ⑤ 格拉姆矩阵: 状态质心 ----------
    F = np.zeros((8, 5))
    for i in range(8):
        m = st == i
        F[i] = Z[m].mean(0)
    G = F @ F.T
    Dm = np.sqrt(np.maximum(np.diag(G)[:, None] + np.diag(G)[None, :] - 2 * G, 0))
    id_err = float(np.abs(Dm - np.sqrt(((F[:, None, :] - F[None, :, :]) ** 2).sum(-1))).max())
    gl, gv = np.linalg.eigh(G)
    R['gram'] = {'matrix': G.tolist(), 'eig': [float(x) for x in gl[::-1]],
                 'identity_max_err': id_err,
                 'rank_eff': float((gl.sum() ** 2) / (gl ** 2).sum())}

    # ---------- ⑥ 投影回归: next_chg ~ prev(5) + 8态哑变量 ----------
    X1 = np.column_stack([A, np.ones(len(A))])
    Dm8 = np.zeros((len(A), 8)); Dm8[np.arange(len(A)), st[:-1][adj[1:]]] = 1.0
    X2 = np.column_stack([A, Dm8[:, 1:], np.ones(len(A))])   # 去掉一维避免共线
    y = B[:, 0]
    b1 = np.linalg.lstsq(X1, y, rcond=None)[0]
    b2 = np.linalg.lstsq(X2, y, rcond=None)[0]
    r1 = y - X1 @ b1; r2 = y - X2 @ b2
    R2_1 = 1 - (r1 @ r1) / ((y - y.mean()) @ (y - y.mean()))
    R2_2 = 1 - (r2 @ r2) / ((y - y.mean()) @ (y - y.mean()))
    # 残差正交性检验: X'r 应≈0
    orth = float(np.abs(X2.T @ r2).max() / len(y))
    # 设计矩阵条件数
    R['projection'] = {
        'R2_prev5': float(R2_1), 'R2_prev5_plus_dummies': float(R2_2),
        'resid_orth_max': orth,
        'cond_X1': float(np.linalg.cond(X1)), 'cond_X2': float(np.linalg.cond(X2)),
        'resid_norm_ratio': float(np.linalg.norm(r2) / np.linalg.norm(y - y.mean()))}

    # ---------- ⑦ 共线性诊断 VIF ----------
    Zc = Z - Z.mean(0)
    vif = []
    for j in range(5):
        others = [k for k in range(5) if k != j]
        Xo = np.column_stack([Zc[:, k] for k in others] + [np.ones(n)])
        bj = np.linalg.lstsq(Xo, Zc[:, j], rcond=None)[0]
        rj = Zc[:, j] - Xo @ bj
        r2j = 1 - (rj @ rj) / (Zc[:, j] @ Zc[:, j])
        vif.append(float(1 / max(1 - r2j, 1e-12)))
    R['vif'] = vif

    # ---------- ⑧ 转移矩阵第二特征向量(振荡模态) ----------
    P = np.array(json.load(open(f'{OUT}/results_matrix.json', encoding='utf-8'))[sym]['trans']['P'])
    w, Vp = np.linalg.eig(P.T)
    i2 = np.argsort(-np.abs(w))[1]
    v2 = np.real(Vp[:, i2]); v2 /= np.abs(v2).max()
    R['P_eigvec2'] = [float(x) for x in v2]
    RES[sym] = R

    print(f'===== {sym} 线性代数视角 =====')
    print('[Σ谱] 特征值:', [round(x, 3) for x in lam], ' 条件数=%.1f' % cond,
          ' 谱熵=%.2fbit 参与率=%.2f' % (sent, pr))
    print('[Γ1] 平方典型相关(逐方向):', [round(x, 3) for x in rho2])
    print('  逐坐标线性可预测R²:', [round(x, 3) for x in r2_coord])
    print('  平均可预测方差 = %.4f' % rho2.mean())
    print('  每PC可预测性:', [round(x, 3) for x in pred_pc])
    lam2 = float(np.sort(np.abs(w))[::-1][1])
    print('[P慢模态] λ2=%.3f 半衰期=%.1f根' % (lam2, np.log(0.5) / np.log(lam2)))
    print('[范数] acf1(‖z‖)=%.3f  acf1(振幅z)=%.3f  ‖z‖均值=%.2f p99=%.2f' % (
        R['norm']['acf1_norm'], R['norm']['acf1_amp_z'], R['norm']['mean_norm'], R['norm']['p99_norm']))
    print('[捕获率] 八态码逐维R²:', [round(x, 3) for x in r2_dims], '均值=%.3f' % np.mean(r2_dims))
    print('  PCA-2=%.3f PCA-3=%.3f' % (R['capture']['r2_pca2'], R['capture']['r2_pca3']))
    print('[格拉姆] 有效秩=%.2f 距离恒等式最大误差=%.2e' % (R['gram']['rank_eff'], id_err))
    print('[投影回归] R²(前5特征)=%.4f → +8态哑变量=%.4f  残差/全变差=%.3f' % (
        R2_1, R2_2, R['projection']['resid_norm_ratio']))
    print('  设计矩阵条件数: X1=%.0f X2=%.0f  残差正交最大偏差=%.2e' % (
        R['projection']['cond_X1'], R['projection']['cond_X2'], orth))
    print('[VIF]', {FEATS[i]: round(vif[i], 1) for i in range(5)})
    print('[P第二特征向量]', [round(x, 2) for x in v2])

json.dump(RES, open(f'{OUT}/results_linalg.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('\nsaved results_linalg.json')
