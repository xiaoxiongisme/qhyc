# -*- coding: utf-8 -*-
"""矩阵视角报告：生成6张widget SVG + matrix_analysis_report.html"""
import json, numpy as np

OUT = '.'
R = json.load(open(f'{OUT}/results_matrix.json', encoding='utf-8'))
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}

INK, SUB, FAINT = '#26215C', '#5F5E5A', '#888780'
LINE = '#D3D1C7'
RED, GREEN = '#C4372E', '#2E7D4F'
RAMP = ['#F6F4EE', '#EAE6F4', '#CFC6E6', '#A397D2', '#6E5FB8', '#3E3480']
PUR = '#5C4FA1'


def ramp(v, vmax):
    i = min(int(v / max(vmax, 1e-9) * (len(RAMP) - 1) + 0.5), len(RAMP) - 1)
    return RAMP[i]


def txt(x, y, s, size=11, fill=SUB, anchor='start', weight='400'):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


# ============ F1 转移矩阵热图 + 谱分析 ============
def fig_transfer(sym='MA'):
    t = R[sym]['trans']
    P = np.array(t['P']) * 100
    cell, x0, y0 = 42, 108, 64
    p = [f'<svg viewBox="0 0 680 400" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'八态转移矩阵 P(下一根形态 | 当前形态) — {sym} 2020-2025', 14, INK, weight='600'))
    p.append(txt(40, 46, '行=当前根形态，列=下一根形态；对角线加框。数值=概率%（48万个相邻分钟对）', 10.5, FAINT))
    for j in range(8):
        p.append(txt(x0 + j * cell + cell / 2, y0 - 8, TRIG[STATES[j]], 10.5, SUB, 'middle'))
    for i in range(8):
        p.append(txt(x0 - 8, y0 + i * cell + cell / 2 + 4, f'{TRIG[STATES[i]]} {STATES[i]}', 10, INK, 'end'))
        for j in range(8):
            v = P[i, j]
            p.append(f'<rect x="{x0 + j * cell}" y="{y0 + i * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="{ramp(v, 26)}"/>')
            if i == j:
                p.append(f'<rect x="{x0 + j * cell}" y="{y0 + i * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="none" stroke="{INK}" stroke-width="1.6"/>')
            dark = v > 13
            p.append(txt(x0 + j * cell + (cell - 2) / 2, y0 + i * cell + cell / 2 + 4, f'{v:.0f}', 10.5,
                         '#FFFFFF' if dark else INK, 'middle', '600' if i == j else '400'))
    # 右侧谱分析面板
    px = 480
    p.append(f'<rect x="{px}" y="60" width="170" height="330" rx="8" fill="#F6F4EE" stroke="{LINE}"/>')
    p.append(txt(px + 12, 82, '谱分析', 12, INK, weight='700'))
    eig = t['eig_abs']
    p.append(txt(px + 12, 102, f'|特征值|: {eig[0]:.2f} {eig[1]:.2f} {eig[2]:.2f} …', 10, SUB))
    p.append(txt(px + 12, 118, f'谱间隙 = {t["spectral_gap"]:.3f}', 10.5, INK, weight='600'))
    p.append(txt(px + 12, 134, f'混合时间 ≈ {t["mixing_bars"]:.1f} 根', 10.5, INK, weight='600'))
    p.append(txt(px + 12, 156, '驻留时间（理论/实测）', 10, SUB))
    for i, s in enumerate(STATES):
        yy = 172 + i * 15
        p.append(txt(px + 12, yy, TRIG[s], 9.5, SUB))
        p.append(txt(px + 62, yy, f'{t["sojourn_theory"][i]:.1f} / {t["sojourn_obs"][i]:.1f}', 9.5, INK))
    p.append(txt(px + 12, 300, f'平稳分布 vs 经验分布', 10, SUB))
    p.append(txt(px + 12, 316, f'最大差 {t["pi_maxdiff"] * 100:.2f} 个百分点', 10.5, INK, weight='600'))
    p.append(txt(px + 12, 338, '马尔可夫假设几乎精确成立', 10, GREEN, weight='600'))
    p.append(txt(px + 12, 356, '但下一根形状基本不可预测', 10, RED, weight='600'))
    p.append('</svg>')
    return ''.join(p)


# ============ F2 互信息矩阵 + 预测MI ============
def fig_mi(sym='MA'):
    m = R[sym]['mi']
    keys, M = m['keys'], np.array(m['matrix'])
    cell, x0, y0 = 44, 132, 66
    p = [f'<svg viewBox="0 0 680 380" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'字段互信息矩阵（比特）— {sym}', 14, INK, weight='600'))
    p.append(txt(40, 46, '同一根K线内部：哪些字段在说同一件事（对角=各自熵）', 10.5, FAINT))
    for j in range(6):
        p.append(txt(x0 + j * cell + cell / 2, y0 - 8, keys[j], 9.5, SUB, 'middle'))
    for i in range(6):
        p.append(txt(x0 - 8, y0 + i * cell + cell / 2 + 4, keys[i], 10, INK, 'end'))
        for j in range(6):
            v = M[i, j]
            p.append(f'<rect x="{x0 + j * cell}" y="{y0 + i * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="{ramp(v, 1.6)}"/>')
            p.append(txt(x0 + j * cell + (cell - 2) / 2, y0 + i * cell + cell / 2 + 4, f'{v:.2f}', 9.5,
                         '#FFFFFF' if v > 0.9 else INK, 'middle'))
    # 右: 预测性MI
    px = 420
    p.append(txt(px, 66, '对下一根的预测性互信息（比特）', 11.5, INK, weight='700'))
    pred = m['pred']
    items = [(k, v['dir'], v['amp']) for k, v in pred.items() if v]
    y = 92
    p.append(txt(px, y, '字段', 9.5, FAINT)); p.append(txt(px + 150, y, '→下一根涨跌', 9.5, FAINT))
    p.append(txt(px + 232, y, '→下一根振幅', 9.5, FAINT))
    y += 8
    for k, d_, a_ in items:
        p.append(txt(px, y + 16, k, 10, INK if k != '形态8态' else PUR, weight='600' if k == '形态8态' else '400'))
        p.append(f'<rect x="{px + 96}" y="{y + 5}" width="{max(d_ * 900, 1):.1f}" height="12" rx="2" fill="{RED}" fill-opacity="0.75"/>')
        p.append(f'<rect x="{px + 96}" y="{y + 20}" width="{max(a_ * 900, 1):.1f}" height="12" rx="2" fill="{PUR}" fill-opacity="0.75"/>')
        p.append(txt(px + 104 + max(d_ * 900, 1), y + 15, f'{d_:.3f}', 9, SUB))
        p.append(txt(px + 104 + max(a_ * 900, 1), y + 30, f'{a_:.3f}', 9, SUB))
        y += 42
    p.append(txt(px, y + 16, '红=预测方向  紫=预测幅度', 9.5, FAINT))
    p.append(txt(px, y + 34, '“当前振幅5档”预测下一根振幅（0.154）', 9.5, INK, weight='600'))
    p.append(txt(px, y + 48, '是形态8态（0.028）的 5.5 倍', 9.5, RED, weight='600'))
    p.append('</svg>')
    return ''.join(p)


# ============ F3 日x时段矩阵 SVD ============
def fig_daymat(sym='MA'):
    dm = R[sym]['daymat']
    p = [f'<svg viewBox="0 0 680 360" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'日 × 时段矩阵（{dm["n_days"]}天 × 345分钟）的奇异值分解 — {sym}', 14, INK, weight='600'))
    p.append(txt(40, 46, f'有效秩 {dm["eff_rank"]:.2f}：第一奇异值独占 {dm["evr_top"][0] * 100:.1f}% 能量——波动率=日内轮廓×日水平的可分离结构', 10.5, FAINT))
    # 左: 奇异值衰减
    x0, y0, bw = 56, 250, 26
    evr = dm['evr_top']
    for i, v in enumerate(evr):
        h = max(np.sqrt(v) * 150, 2)
        p.append(f'<rect x="{x0 + i * (bw + 8)}" y="{y0 - h:.1f}" width="{bw}" height="{h:.1f}" rx="3" fill="{ramp(v, 0.75)}"/>')
        p.append(txt(x0 + i * (bw + 8) + bw / 2, y0 - h - 6, f'{v * 100:.1f}%', 9.5, SUB, 'middle'))
        p.append(txt(x0 + i * (bw + 8) + bw / 2, y0 + 16, f'σ{i + 1}', 9.5, FAINT, 'middle'))
    p.append(txt(x0 + 3 * (bw + 8), y0 + 34, '前6个奇异值的能量占比', 10, SUB))
    p.append(f'<line x1="46" y1="{y0}" x2="330" y2="{y0}" stroke="{LINE}"/>')
    # 右: 日内轮廓
    px, pw, py, ph = 380, 260, 80, 190
    prof = dm['profile3']
    cols = [PUR, '#C77F3E', '#2F7E7A']
    names = [f'轮廓1（{evr[0] * 100:.0f}%）开盘尖峰×日内节奏', f'轮廓2（{evr[1] * 100:.0f}%）日盘/夜盘对比', f'轮廓3（{evr[2] * 100:.1f}%）尾盘结构']
    for k in range(3):
        v = np.array(prof[k]); v = v / np.abs(v).max()
        pts = ' '.join(f'{px + i * pw / (len(v) - 1):.1f},{py + ph / 2 - v[i] * ph / 2 * 0.9:.1f}' for i in range(len(v)))
        p.append(f'<polyline points="{pts}" fill="none" stroke="{cols[k]}" stroke-width="1.6"/>')
    for b, lab in [(15, '10:15'), (27, '11:30'), (45, '15:00')]:
        x = px + b * pw / 69
        p.append(f'<line x1="{x:.1f}" y1="{py - 6}" x2="{x:.1f}" y2="{py + ph + 6}" stroke="{LINE}" stroke-dasharray="2,2"/>')
        p.append(txt(x, py + ph + 18, lab, 8.5, FAINT, 'middle'))
    p.append(txt(px, py - 12, '09:00', 8.5, FAINT))
    p.append(txt(px + pw, py - 12, '23:59', 8.5, FAINT, 'end'))
    for k in range(3):
        p.append(f'<rect x="{px}" y="{py + ph + 28 + k * 16}" width="10" height="3" fill="{cols[k]}"/>')
        p.append(txt(px + 16, py + ph + 33 + k * 16, names[k], 9, SUB))
    p.append('</svg>')
    return ''.join(p)


# ============ F4 状态距离矩阵 + 聚类 ============
def fig_dist(sym='MA'):
    d = R[sym]['dist']
    D = np.array(d['D']); order = d['order']
    cell, x0, y0 = 46, 128, 70
    p = [f'<svg viewBox="0 0 680 400" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'八态距离矩阵（形态特征空间欧氏距离，按聚类重排）— {sym}', 14, INK, weight='600'))
    p.append(txt(40, 46, '特征=平均涨跌/振幅/CLV/上影/下影。颜色越深=差异越大；白框=错卦对，虚线框=综卦对', 10.5, FAINT))
    for j, i in enumerate(order):
        p.append(txt(x0 + j * cell + cell / 2, y0 - 8, TRIG[STATES[i]], 10, SUB, 'middle'))
    for r, i in enumerate(order):
        p.append(txt(x0 - 8, y0 + r * cell + cell / 2 + 4, f'{TRIG[STATES[i]]} {STATES[i]}', 9.5, INK, 'end'))
        for c, j in enumerate(order):
            v = D[i, j]
            p.append(f'<rect x="{x0 + c * cell}" y="{y0 + r * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="{ramp(v, 5)}"/>')
            if i != j:
                isz = (i, j) in [(2, 1), (5, 6)] or (j, i) in [(2, 1), (5, 6)]
                isc = (i, j) in [(3, 4), (0, 7), (2, 5), (1, 6)] or (j, i) in [(3, 4), (0, 7), (2, 5), (1, 6)]
                if isc:
                    p.append(f'<rect x="{x0 + c * cell}" y="{y0 + r * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="none" stroke="#FFFFFF" stroke-width="1.8"/>')
                elif isz:
                    p.append(f'<rect x="{x0 + c * cell}" y="{y0 + r * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="none" stroke="{INK}" stroke-width="1.2" stroke-dasharray="3,2"/>')
            p.append(txt(x0 + c * cell + (cell - 2) / 2, y0 + r * cell + cell / 2 + 4, f'{v:.1f}', 9,
                         '#FFFFFF' if v > 3.2 else INK, 'middle'))
    # 底部对比条
    by = 470 - 60
    bars = [('其他对（差1个字段）', d['d_other'], FAINT), ('综卦对（影线换边）', d['d_zong'], PUR), ('错卦对（三爻全反）', d['d_cuo'], RED)]
    p.append(txt(40, by - 8, '平均距离对比', 11, INK, weight='700'))
    for i, (lab, v, col) in enumerate(bars):
        yy = by + 14 + i * 24
        p.append(txt(40, yy + 10, lab, 10, SUB))
        p.append(f'<rect x="200" y="{yy}" width="{v * 90:.0f}" height="14" rx="3" fill="{col}" fill-opacity="0.8"/>')
        p.append(txt(206 + v * 90, yy + 11, f'{v:.2f}', 9.5, INK, weight='600'))
    p.append(txt(470, by + 14, '聚类读出真实结构：', 10, INK, weight='600'))
    p.append(txt(470, by + 32, '阳线4态（乾巽兑坎）与阴线4态', 9.5, SUB))
    p.append(txt(470, by + 46, '（离震艮坤）先分家，家族内', 9.5, SUB))
    p.append(txt(470, by + 60, '再按影线模式（无/下/上/双）排', 9.5, SUB))
    p.append(txt(470, by + 78, '“综卦=小变动”在数据里不成立', 9.5, RED, weight='600'))
    p.append('</svg>')
    return ''.join(p)


# ============ F5 PCA ============
def fig_pca(sym='MA'):
    pc = R[sym]['pca']
    feats = ['涨跌幅', '振幅', 'CLV', '上影', '下影']
    p = [f'<svg viewBox="0 0 680 340" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'形状向量 PCA：一根K线在特征空间里是几维物体 — {sym}', 14, INK, weight='600'))
    p.append(txt(40, 46, '输入=标准化后的(涨跌幅,振幅,CLV,上影,下影)，30万根随机样本', 10.5, FAINT))
    # 左 scree
    x0, y0, bw = 56, 240, 40
    for i, v in enumerate(pc['evr']):
        h = max(v * 420, 2)
        p.append(f'<rect x="{x0 + i * (bw + 10)}" y="{y0 - h:.1f}" width="{bw}" height="{h:.1f}" rx="3" fill="{ramp(v, 0.45)}"/>')
        p.append(txt(x0 + i * (bw + 10) + bw / 2, y0 - h - 6, f'{v * 100:.1f}%', 9.5, SUB, 'middle'))
        p.append(txt(x0 + i * (bw + 10) + bw / 2, y0 + 16, f'PC{i + 1}', 9.5, FAINT, 'middle'))
    p.append(f'<line x1="46" y1="{y0}" x2="330" y2="{y0}" stroke="{LINE}"/>')
    p.append(txt(56, y0 + 36, f'PC1+PC2+PC3 = {pc["cum3"] * 100:.0f}% 方差', 10.5, INK, weight='600'))
    # 右 loadings 5x3
    L = np.array(pc['loadings']).T  # 5 x 3
    px, py, cw, ch = 400, 70, 56, 40
    for j in range(3):
        p.append(txt(px + j * cw + cw / 2, py - 8, f'PC{j + 1}', 10, SUB, 'middle'))
    for i in range(5):
        p.append(txt(px - 8, py + i * ch + ch / 2 + 4, feats[i], 10, INK, 'end'))
        for j in range(3):
            v = L[i, j]
            col = '#2F7E7A' if v < 0 else '#8C4FA1'
            alpha = min(abs(v) * 1.6, 0.85)
            p.append(f'<rect x="{px + j * cw}" y="{py + i * ch}" width="{cw - 2}" height="{ch - 2}" rx="4" fill="{col}" fill-opacity="{alpha:.2f}"/>')
            p.append(txt(px + j * cw + (cw - 2) / 2, py + i * ch + ch / 2 + 4, f'{v:+.2f}', 9.5, '#FFFFFF' if alpha > 0.5 else INK, 'middle'))
    p.append(txt(px, py + 5 * ch + 22, 'PC1=收盘位置轴  PC2=方向轴', 10, INK, weight='600'))
    p.append(txt(px, py + 5 * ch + 38, 'PC3=大小轴（振幅独占）', 10, INK, weight='600'))
    p.append(txt(px, py + 5 * ch + 58, '六位码 = PC2的1比特 + PC1的2比特', 9.5, RED, weight='600'))
    p.append(txt(px, py + 5 * ch + 72, '          + PC3的 0 比特（幅度被丢掉）', 9.5, RED, weight='600'))
    p.append('</svg>')
    return ''.join(p)


# ============ F6 MA x EG 交叉矩阵 ============
def fig_cross():
    c = R['cross']
    p = [f'<svg viewBox="0 0 680 350" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, '甲醇 × 乙二醇：交叉相关与联合占用矩阵', 14, INK, weight='600'))
    p.append(txt(40, 46, f'同刻样本 {c["n_common"]:,} 根；k=甲醇领先k分钟（负=乙二醇领先）', 10.5, FAINT))
    # 左 lead-lag
    px, py, pw, ph = 60, 80, 250, 150
    ks = list(range(-10, 11))
    ch = [c['lead']['chg'][str(k)] for k in ks]
    am = [c['lead']['amp'][str(k)] for k in ks]
    vmax = max(float(np.abs(np.array(ch)).max()), float(np.abs(np.array(am)).max()), 1e-6)
    p.append(f'<line x1="{px}" y1="{py + ph / 2}" x2="{px + pw}" y2="{py + ph / 2}" stroke="{LINE}"/>')
    for k, v in zip(ks, ch):
        x = px + (k + 10) * pw / 20
        y = py + ph / 2 - v / vmax * ph / 2 * 0.9
        col = RED if v >= 0 else GREEN
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{col}"/>')
    for k, v in zip(ks, am):
        x = px + (k + 10) * pw / 20
        y = py + ph / 2 - v / vmax * ph / 2 * 0.9
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="none" stroke="{PUR}" stroke-width="1.4"/>')
    pts_ch = ' '.join(f'{px + (k + 10) * pw / 20:.1f},{py + ph / 2 - v / vmax * ph / 2 * 0.9:.1f}' for k, v in zip(ks, ch))
    pts_am = ' '.join(f'{px + (k + 10) * pw / 20:.1f},{py + ph / 2 - v / vmax * ph / 2 * 0.9:.1f}' for k, v in zip(ks, am))
    p.append(f'<polyline points="{pts_ch}" fill="none" stroke="{RED}" stroke-width="1.2" stroke-opacity="0.55"/>')
    p.append(f'<polyline points="{pts_am}" fill="none" stroke="{PUR}" stroke-width="1.2" stroke-opacity="0.55"/>')
    for k in [-10, -5, 0, 5, 10]:
        p.append(txt(px + (k + 10) * pw / 20, py + ph + 16, f'{k:+d}' if k else '0', 9, FAINT, 'middle'))
    p.append(f'<line x1="{px + 10 * pw / 20:.1f}" y1="{py - 6}" x2="{px + 10 * pw / 20:.1f}" y2="{py + ph + 6}" stroke="{INK}" stroke-dasharray="2,2"/>')
    p.append(txt(px, py - 12, '滞后k（分钟）', 9.5, FAINT))
    p.append(f'<rect x="{px}" y="{py + ph + 26}" width="10" height="3" fill="{RED}"/>')
    p.append(txt(px + 16, py + ph + 31, '涨跌幅互相关', 9, SUB))
    p.append(f'<circle cx="{px + 5}" cy="{py + ph + 46}" r="3" fill="none" stroke="{PUR}" stroke-width="1.4"/>')
    p.append(txt(px + 16, py + ph + 50, '振幅互相关', 9, SUB))
    p.append(txt(px, py + ph + 70, f'峰值都在 k=0：同步共动，无领先关系', 9.5, INK, weight='600'))
    p.append(txt(px, py + ph + 84, f'同刻相关: 涨跌幅 {c["corr_chg"]:.3f}  振幅 {c["corr_amp"]:.3f}', 9.5, SUB))
    # 右 联合占用 8x8
    J = np.array(c['joint'], dtype=float)
    J = J / J.sum() * 100
    gx, gy, cell = 420, 84, 28
    for j in range(8):
        p.append(txt(gx + j * cell + cell / 2, gy - 6, TRIG[STATES[j]][0], 9, SUB, 'middle'))
    for i in range(8):
        p.append(txt(gx - 6, gy + i * cell + cell / 2 + 3, TRIG[STATES[i]], 9, INK, 'end'))
        for j in range(8):
            v = J[i, j]
            p.append(f'<rect x="{gx + j * cell}" y="{gy + i * cell}" width="{cell - 1}" height="{cell - 1}" rx="2" fill="{ramp(v, 6.5)}"/>')
    p.append(txt(gx, gy - 20, '联合占用矩阵 P(甲醇态, 乙二醇态) %', 9.5, INK, weight='600'))
    p.append(txt(gx, gy + 8 * cell + 18, f'行=甲醇  列=乙二醇', 9, FAINT))
    p.append(txt(gx, gy + 8 * cell + 34, f'联合互信息 {c["joint_MI"]:.3f} 比特', 10, INK, weight='600'))
    p.append(txt(gx, gy + 8 * cell + 48, f'（置换零假设 {c["joint_MI_null"]:.4f}，p={c["joint_MI_p"]:.3f}）', 9, SUB))
    p.append(txt(gx, gy + 8 * cell + 64, f'最高共现: 坎☵-坎☵ {J[7, 7]:.1f}%（同时刻高波动）', 9.5, SUB))
    p.append('</svg>')
    return ''.join(p)


figs = {'transfer': fig_transfer('MA'), 'mi': fig_mi('MA'), 'daymat': fig_daymat('MA'),
        'dist': fig_dist('MA'), 'pca': fig_pca('MA'), 'cross': fig_cross()}


# ============ F7 协方差矩阵与谱 ============
def fig_sigma(sym='MA'):
    L = json.load(open(f'{OUT}/results_linalg.json', encoding='utf-8'))[sym]
    S = np.array(L['Sigma']['matrix'])
    lam = L['Sigma']['eig']
    cell, x0, y0 = 52, 128, 74
    p = [f'<svg viewBox="0 0 680 340" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'协方差矩阵 Σ（标准化后的 5×5 相关矩阵）— {sym}', 14, INK, weight='600'))
    p.append(txt(40, 46, '矩阵的谱决定“一根K线实际是几维物体”；条件数/谱熵/参与率是三个刻度', 10.5, FAINT))
    feats = ['涨跌幅', '振幅', 'CLV', '上影', '下影']
    for j in range(5):
        p.append(txt(x0 + j * cell + cell / 2, y0 - 8, feats[j], 9.5, SUB, 'middle'))
    for i in range(5):
        p.append(txt(x0 - 8, y0 + i * cell + cell / 2 + 4, feats[i], 10, INK, 'end'))
        for j in range(5):
            v = S[i, j]
            if i == j:
                p.append(f'<rect x="{x0 + j * cell}" y="{y0 + i * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="#3E3480"/>')
                p.append(txt(x0 + j * cell + (cell - 2) / 2, y0 + i * cell + cell / 2 + 4, '1', 10, '#FFFFFF', 'middle'))
            else:
                col = RED if v > 0 else GREEN
                p.append(f'<rect x="{x0 + j * cell}" y="{y0 + i * cell}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="{col}" fill-opacity="{min(abs(v) * 1.1, 0.8):.2f}"/>')
                p.append(txt(x0 + j * cell + (cell - 2) / 2, y0 + i * cell + cell / 2 + 4, f'{v:+.2f}', 9.5,
                             '#FFFFFF' if abs(v) > 0.55 else INK, 'middle'))
    # 右: 谱
    px = 420
    p.append(txt(px, 74, '特征值谱', 11.5, INK, weight='700'))
    for i, v in enumerate(lam):
        yy = 92 + i * 24
        p.append(txt(px, yy + 10, f'λ{i + 1}', 10, SUB))
        p.append(f'<rect x="{px + 34}" y="{yy}" width="{v * 90:.0f}" height="14" rx="3" fill="{ramp(v, 2)}"/>')
        p.append(txt(px + 40 + v * 90, yy + 11, f'{v:.2f}', 9.5, INK, weight='600'))
    p.append(f'<line x1="{px + 34}" y1="212" x2="{px + 220}" y2="212" stroke="{LINE}"/>')
    p.append(txt(px, 232, f'条件数 = {L["Sigma"]["cond"]:.1f}', 10.5, INK, weight='600'))
    p.append(txt(px, 250, f'谱熵 = {L["Sigma"]["spec_entropy_bits"]:.2f} bit（上限2.32）', 10, SUB))
    p.append(txt(px, 266, f'参与率（有效维度）= {L["Sigma"]["participation_ratio"]:.2f}', 10, INK, weight='600'))
    p.append(txt(px, 288, '解读：5个坐标没有严格共线，', 9.5, SUB))
    p.append(txt(px, 302, '但有效维度只有 3.7——真正', 9.5, SUB))
    p.append(txt(px, 316, '“活着”的主轴是前 3 个', 9.5, SUB))
    p.append('</svg>')
    return ''.join(p)


# ============ F8 可预测性与捕获率 ============
def fig_pred(sym='MA'):
    L = json.load(open(f'{OUT}/results_linalg.json', encoding='utf-8'))[sym]
    feats = ['涨跌幅', '振幅', 'CLV', '上影', '下影']
    r2c = L['Gamma1']['r2_coord']
    cap = L['capture']['r2_code8_per_dim']
    p = [f'<svg viewBox="0 0 680 340" width="100%" xmlns="http://www.w3.org/2000/svg">']
    p.append(txt(40, 26, f'逐坐标可预测性 vs 六位码捕获率 — {sym}', 14, INK, weight='600'))
    p.append(txt(40, 46, '左：下一根同坐标的线性可预测 R²（滞后协方差的对角/Σ对角）。右：八态码分组在各维捕获的方差份额', 10.5, FAINT))
    # 左: 逐坐标R²
    px, py, bw = 60, 220, 34
    for i, v in enumerate(r2c):
        h = max(v * 500, 1.5)
        p.append(f'<rect x="{px + i * (bw + 14)}" y="{py - h:.1f}" width="{bw}" height="{h:.1f}" rx="3" fill="{RED if i == 1 else '#A397D2'}" fill-opacity="{0.9 if i == 1 else 0.75}"/>')
        p.append(txt(px + i * (bw + 14) + bw / 2, py - h - 6, f'{v * 100:.1f}%', 9.5, SUB, 'middle'))
        p.append(txt(px + i * (bw + 14) + bw / 2, py + 16, feats[i], 9.5, FAINT, 'middle'))
    p.append(f'<line x1="46" y1="{py}" x2="330" y2="{py}" stroke="{LINE}"/>')
    p.append(txt(px, py + 38, '只有“振幅”坐标显著可预测（23.6%）', 10, INK, weight='600'))
    p.append(txt(px, py + 54, '涨跌方向 2.2%、CLV 0.1%——近乎白噪声', 9.5, SUB))
    p.append(txt(px, py + 72, f'PC3（大小轴）可预测性 {L["Gamma1"]["pred_per_pc"][2] * 100:.0f}% 全场最高', 9.5, RED, weight='600'))
    # 右: 捕获率
    gx, gy = 420, 220
    for i, v in enumerate(cap):
        h = max(v * 190, 1.5)
        p.append(f'<rect x="{gx + i * 40}" y="{gy - h:.1f}" width="30" height="{h:.1f}" rx="3" fill="{ramp(v, 0.9)}"/>')
        p.append(txt(gx + i * 40 + 15, gy - h - 6, f'{v * 100:.0f}%', 9.5, SUB, 'middle'))
        p.append(txt(gx + i * 40 + 15, gy + 16, feats[i], 9, FAINT, 'middle'))
    y3 = gy - L['capture']['r2_pca3'] * 190
    p.append(f'<line x1="{gx - 6}" y1="{y3:.1f}" x2="{gx + 205}" y2="{y3:.1f}" stroke="{RED}" stroke-width="1.4" stroke-dasharray="4,3"/>')
    p.append(txt(gx + 208, y3 + 4, f'PCA-3 = {L["capture"]["r2_pca3"] * 100:.0f}%', 9.5, RED, weight='600'))
    p.append(f'<line x1="46" y1="{gy}" x2="{gx + 205}" y2="{gy}" stroke="{LINE}"/>')
    p.append(txt(gx, gy + 38, '六位码在“振幅”维只捕获 11.9%', 10, RED, weight='600'))
    p.append(txt(gx, gy + 54, '（VIF=1.0，全场唯一正交的坐标）', 9.5, SUB))
    p.append(txt(gx, gy + 72, 'CLV/影线维捕获 74~84%——码的“主场”', 9.5, SUB))
    p.append('</svg>')
    return ''.join(p)


figs['sigma'] = fig_sigma('MA')
figs['pred'] = fig_pred('MA')
for k, v in figs.items():
    open(f'_w_{k}.svg', 'w', encoding='utf-8').write(v)
    print(k, len(v), 'chars,', v.count('<rect'), 'rects,', v.count('<text'), 'texts')
