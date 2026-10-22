# -*- coding: utf-8 -*-
"""多维度分析图表生成: 8 张手写 SVG"""
import json, numpy as np

R = json.load(open('results_dimensions.json', encoding='utf-8'))
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
INK, SUB, MUT = '#26215C', '#5F5E5A', '#888780'
P1, P2, P3, P4 = '#3E3480', '#6E5FB8', '#A397D2', '#CFC6E6'
BG, RED, GRN = '#F6F4EE', '#C4372E', '#2E7D4F'


def sv(h):
    return f'<svg viewBox="0 0 680 {h}" width="100%" xmlns="http://www.w3.org/2000/svg">\n'


def T(x, y, s, size=10, fill=INK, anchor='start', weight=400):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>\n')


def B(x, y, w, h, fill, op=1.0, rx=3):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" height="{max(h,0):.1f}" rx="{rx}" fill="{fill}" fill-opacity="{op}"/>\n'


def L(x1, y1, x2, y2, c='#D3D1C7', w=1, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="{w}"{d}/>\n'


def PL(pts, c, w=1.5, op=1.0):
    p = ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)
    return f'<polyline points="{p}" fill="none" stroke="{c}" stroke-width="{w}" stroke-opacity="{op}"/>\n'


def head(title, sub, h=26):
    return T(40, h, title, 14, INK, weight=600) + T(40, h + 18, sub, 10.5, MUT)


MA, EG = R['MA'], R['EG']

# ================= 图1 信息论/压缩 =================
e = MA['entropy']; ee = EG['entropy']
s = sv(300) + head('维度一 · 信息论与压缩：六位码每根只携带 2.72 比特', '熵率 = 块熵 H(下一态 | 前三态)；对比 6 bit 编码上限')
bars = [('H(当前状态)', e['H_state'], P2), ('H(下一态 | 当前态)', e['block']['1'], P2),
        ('块熵 k=2', e['block']['2'], P3), ('块熵 k=3（熵率）', e['block']['3'], P1)]
x0, sc = 150, 26
for i, (lab, v, c) in enumerate(bars):
    y = 78 + i * 34
    s += T(140, y + 14, lab, 10, INK, 'end') + B(x0, y, v * sc, 20, c) + T(x0 + v * sc + 6, y + 14, f'{v:.3f} bit', 10, INK, weight=600)
s += L(x0, 78, x0, 214, MUT, 1) + T(x0, 232, '0', 9, MUT, 'middle') + T(x0 + 3 * sc, 232, '3', 9, MUT, 'middle')
s += L(x0 + 6 * sc, 70, x0 + 6 * sc, 214, RED, 1.4, '4,3') + T(x0 + 6 * sc, 66, '六位码 = 6 bit', 9.5, RED, 'middle', 600)
s += T(40, 262, f'压缩率 = 2.722 / 6 = 45.4%   有效字母表 = e^2.93 ≈ 18.7 个等价态', 10.5, INK, weight=600)
s += T(40, 280, 'EG 复验: 熵率 2.780 bit（46.3%），两品种一致', 9.5, MUT)
# 右: 分年熵率
yrs = sorted(int(y) for y in e['by_year'])
vx = lambda i: 430 + i * 30
vy = lambda v: 200 - (v - 2.5) / 0.5 * 130
s += T(430, 60, '分年熵率（2020–2026）', 10.5, INK, weight=700)
for gv in [2.5, 2.6, 2.7, 2.8]:
    s += L(420, vy(gv), 645, vy(gv), '#E8E6DE', 1) + T(414, vy(gv) + 3, f'{gv:.1f}', 9, MUT, 'end')
for sym, rr, c in [('MA', e, P1), ('EG', ee, RED)]:
    pts = [(vx(i), vy(rr['by_year'][str(y)])) for i, y in enumerate(yrs)]
    s += PL(pts, c, 1.8) + ''.join(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="2.8" fill="{c}"/>' for p in pts)
for i, y in enumerate(yrs):
    s += T(vx(i), 216, str(y)[2:], 9, MUT, 'middle')
s += B(430, 236, 9, 9, P1) + T(443, 244, 'MA', 9, SUB) + B(470, 236, 9, 9, RED) + T(483, 244, 'EG', 9, SUB)
s += T(430, 268, '熵率七年稳定在 2.63–2.80 bit：', 10, INK, weight=600)
s += T(430, 282, '结构不随时间退化，也无趋势', 9.5, SUB)
s += '</svg>'
open('_d_entropy.svg', 'w', encoding='utf-8').write(s)

# ================= 图2 记忆深度 =================
m, mm = MA['memory'], EG['memory']
s = sv(310) + head('维度二 · 记忆深度：二阶马尔可夫仍有增量，三阶起饱和', '2020–2025 训练 → 2026 holdout 平均 log2 损失（比特/根，越低越好）')
ords = ['0', '1', '2', '3']
x0, bw = 90, 34
lo, hi = 2.60, 3.00
vy = lambda v: 240 - (v - lo) / (hi - lo) * 160
for gv in [2.6, 2.7, 2.8, 2.9, 3.0]:
    s += L(80, vy(gv), 330, vy(gv), '#E8E6DE', 1) + T(74, vy(gv) + 3, f'{gv:.1f}', 9, MUT, 'end')
for i, o in enumerate(ords):
    for j, (rr, c) in enumerate([(m, P1), (mm, RED)]):
        v = rr['holdout'][o]['loss']
        bx = x0 + i * 60 + j * bw
        s += B(bx, vy(v), bw - 6, 240 - vy(v), c) + T(bx + (bw - 6) / 2, vy(v) - 5, f'{v:.3f}', 9, INK, 'middle', 600)
    s += T(x0 + i * 60 + bw, 258, f'{o} 阶', 10, SUB, 'middle')
s += B(90, 278, 9, 9, P1) + T(103, 286, 'MA', 9, SUB) + B(130, 278, 9, 9, RED) + T(143, 286, 'EG', 9, SUB)
s += T(200, 286, '增益: 一阶 +0.066 / 二阶 +0.041 / 三阶 +0.013 bit', 9.5, INK, weight=600)
# 右: top1 准确率
s += T(430, 60, '一阶模型的 top-1 猜中率 vs 持续性基线', 10.5, INK, weight=700)
rows = [('MA 一阶马尔可夫', m['order1_top1_acc'], P1), ('MA 持续性(猜不变)', m['persistence_acc'], P4),
        ('EG 一阶马尔可夫', mm['order1_top1_acc'], RED), ('EG 持续性(猜不变)', mm['persistence_acc'], '#E5B8B0')]
for i, (lab, v, c) in enumerate(rows):
    y = 84 + i * 40
    s += T(424, y + 13, lab, 10, INK, 'end') + B(430, y, v * 700, 20, c) + T(430 + v * 700 + 6, y + 14, f'{v*100:.1f}%', 10, INK, weight=600)
s += T(430, 262, '知道当前卦象，猜中率 24–28%', 10, INK, weight=600)
s += T(430, 278, '（随机 12.5%，持续性 17–19%）', 9.5, SUB)
s += T(430, 296, '→ 形状信息真实存在，但不足以稳定预测', 9.5, RED, weight=600)
s += '</svg>'
open('_d_order.svg', 'w', encoding='utf-8').write(s)

# ================= 图3 图论/卦络 =================
g = MA['graph']; P = np.array(g['P']); pr = np.array(g['pagerank'])
s = sv(370) + head('维度三 · 图论：转移图的社区结构就是「阴阳两族」', '节点大小 ∝ PageRank；紫实线=显著偏好边(z>3)，绿虚线=显著回避边(z<-3)，灰细线=其余边')
UP = [4, 5, 6, 7]   # 阳(涨): 乾巽兑坎
DN = [0, 1, 2, 3]   # 阴(跌): 离艮震坤
pos = {}
for i, stt in enumerate(UP):
    pos[stt] = (140 + i * 130, 110)
for i, stt in enumerate(DN):
    pos[stt] = (140 + i * 130, 258)
s += B(90, 66, 520, 96, P4, 0.35, 10) + B(90, 214, 520, 96, '#DCE8F0', 0.5, 10)
s += T(96, 80, '阳族（实体向上 4 态）', 9.5, P1, weight=600) + T(96, 228, '阴族（实体向下 4 态）', 9.5, '#3A6A8A', weight=600)
z = np.array(g['z_matrix'])
for i in range(8):
    for j in range(8):
        if i == j:
            continue
        p = P[i, j]
        if p < 0.02:
            continue
        x1, y1 = pos[i]; x2, y2 = pos[j]
        if z[i, j] > 3:
            s += L(x1, y1, x2, y2, P1, 0.6 + 2.2 * p, )
        elif z[i, j] < -3:
            s += L(x1, y1, x2, y2, GRN, 0.5 + 1.6 * p, '3,3')
        else:
            s += L(x1, y1, x2, y2, '#C9C7BD', 0.4 + 1.6 * p)
for i in range(8):
    x, y = pos[i]
    r = 16 + pr[i] * 90
    s += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{P2 if i in UP else "#5E84A8"}" stroke="#FFFFFF" stroke-width="1.6"/>'
    s += T(x, y - 2, TRIG[STATES[i]], 11, '#FFFFFF', 'middle', 600)
    s += T(x, y + 11, STATES[i], 7.5, '#FFFFFF', 'middle')
    s += T(x, y + 24, f'PR={pr[i]:.3f}', 8, MUT, 'middle')
s += T(40, 330, f'64 条边中 {g["n_sig_edges"]} 条显著（偏好 {g["n_sig_pos"]} / 回避 {g["n_sig_neg"]}）　强连通=是（任意态可达任意态）', 10, INK, weight=600)
s += T(40, 346, f'贪心模块度社区 = [离艮震坤 | 乾巽兑坎]，Q={g["modularity"]:.3f}　加权平均路径 = {g["mean_path"]:.2f} bit', 10, INK, weight=600)
s += T(40, 362, '距离矩阵的「两家族」在图拓扑中独立复现；EG 社区划分逐位相同', 9.5, RED, weight=600)
s += '</svg>'
open('_d_graph.svg', 'w', encoding='utf-8').write(s)

# ================= 图4 频域/节律 =================
f = MA['freq']; ff = EG['freq']
s = sv(350) + head('维度四 · 频域与节律：卦气有日内节律，也有「周一效应」', '左: 日内振幅轮廓(5分钟块, MA)；右上: 星期一–星期五均振幅；右下: 月均振幅')
prof = f['profile69']
px = lambda i: 52 + i * 4.0
py = lambda v: 208 - v / 0.27 * 142
for gv in [0.05, 0.10, 0.15, 0.20, 0.25]:
    s += L(48, py(gv), 328, py(gv), '#E8E6DE', 1) + T(44, py(gv) + 3, f'{gv*100:.0f}%', 9, MUT, 'end')
for a, b_, lab in [(0, 14, '早盘 9:00'), (15, 26, '午盘 10:15'), (27, 44, '下午 13:30'), (45, 68, '夜盘 21:00')]:
    s += B(px(a), 66, px(b_) - px(a) + 4, 148, P4 if lab != '夜盘 21:00' else '#DCE8F0', 0.35, 2)
    s += T((px(a) + px(b_)) / 2, 216, lab, 8.5, SUB, 'middle')
s += PL([(px(i), py(v)) for i, v in enumerate(prof)], P1, 1.8)
s += T(px(0) + 4, py(prof[0]) + 10, f'开盘块 {prof[0]*100:.2f}%', 8.5, P1, 'start', 600)
s += T(px(68), py(prof[68]) - 8, f'{prof[68]*100:.2f}%', 8.5, P1, 'end', 600)
s += T(px(14), py(prof[14]) - 8, f'{prof[14]*100:.2f}%', 8.5, P1, 'middle', 600)
s += T(40, 238, f'开盘段占比 {f["open_slot_share"]:.2f}×　夜盘占比 {f["night_share"]:.2f}×　日内谱峰周期 115 / 172.5 分钟', 9.5, INK, weight=600)
s += T(40, 254, '（115 分钟 ≈ 单段会话长度：节律来自开收盘结构，非神秘周期）', 9, MUT)
# 右上: 周一效应
s += T(420, 66, '星期效应（平均振幅）', 10.5, INK, weight=700)
dw = ['周一', '周二', '周三', '周四', '周五']
for i in range(5):
    y = 84 + i * 30
    v1, v2 = f['dow_mean'][i], ff['dow_mean'][i]
    s += T(414, y + 13, dw[i], 10, INK, 'end', 600 if i == 0 else 400)
    s += B(420, y, v1 / 0.14 * 130, 11, P1 if i else RED) + T(420 + v1 / 0.14 * 130 + 5, y + 9, f'{v1*100:.2f}%', 8.5, SUB)
    s += B(420, y + 12, v2 / 0.14 * 130, 11, '#E5B8B0' if i == 0 else P3) + T(420 + v2 / 0.14 * 130 + 5, y + 21, f'{v2*100:.2f}%', 8.5, MUT)
s += B(420, 240, 9, 9, P1) + T(433, 248, 'MA', 9, SUB) + B(460, 240, 9, 9, P3) + T(473, 248, 'EG', 9, SUB)
s += T(500, 248, '红=周一', 9, RED, weight=600)
s += T(420, 272, f'周一振幅仅为周二~周五的 {f["dow_mean"][0]/np.mean(f["dow_mean"][1:]):.2f} 倍（EG {ff["dow_mean"][0]/np.mean(ff["dow_mean"][1:]):.2f}）', 10, RED, weight=600)
s += T(420, 288, '星期×时段矩阵 σ₁ 占 72%：星期效应几乎是单一因子', 9.5, SUB)
# 右下: 月度
s += T(420, 312, '月均振幅（%）：三月最高 / 八月最低，峰谷比 1.39，F=891（p<1e-16）', 9.5, INK, weight=600)
s += T(420, 328, '日振幅谱无可交易周期（红噪声型）；季节性用于仓位日历，不用于择时', 9, MUT)
s += '</svg>'
open('_d_freq.svg', 'w', encoding='utf-8').write(s)

# ================= 图5 稳定性/变点 =================
st, se = MA['stability'], EG['stability']
s = sv(330) + head('维度五 · 结构稳定性：转移矩阵逐年漂移，但 2026 不比普通年份更怪', '左: 各年转移矩阵相对全样本的 Frobenius 相对差；右: CUSUM 变点监测 + 惊讶度')
yrs = sorted(int(y) for y in st['Fro_by_year'])
x0 = 60
vy = lambda v: 220 - v / 0.35 * 150
for gv in [0, 0.1, 0.2, 0.3]:
    s += L(52, vy(gv), 320, vy(gv), '#E8E6DE', 1) + T(48, vy(gv) + 3, f'{gv:.1f}', 9, MUT, 'end')
for i, y in enumerate(yrs):
    v1 = st['Fro_by_year'][str(y)]; v2 = se['Fro_by_year'][str(y)]
    bx = x0 + i * 36
    s += B(bx, vy(v1), 15, 220 - vy(v1), P1 if y != 2026 else RED) + B(bx + 16, vy(v2), 15, 220 - vy(v2), P3 if y != 2026 else '#E5B8B0')
    s += T(bx + 15, 236, str(y)[2:], 9, SUB, 'middle')
    if y == 2026:
        s += T(bx + 15, vy(v1) - 5, f'{v1:.2f}', 8.5, RED, 'middle', 600)
s += L(52, vy(st['Fro_2026_vs_train']), 320, vy(st['Fro_2026_vs_train']), RED, 1.2, '4,3')
s += T(322, vy(st['Fro_2026_vs_train']) - 5, f'2026 vs 训练 = {st["Fro_2026_vs_train"]:.2f}', 8.5, RED, 'end', 600)
s += T(40, 258, '跨年漂移 0.08–0.33，量级小且无单调趋势', 10, INK, weight=600)
s += T(40, 274, f'χ² 齐性检验 p<1e-16（样本 50 万+，小差异也显著）', 9.5, SUB)
# 右: 逐窗口 z + logloss
s += T(420, 60, '震☳→震☳ 条件概率：逐窗口偏离 z（窗口=2000根）', 10.5, INK, weight=700)
cus = st['cusum_edge']
xs = [c[0] for c in cus]
zw = [c[1] for c in cus]
cx = lambda v: 430 + (v - xs[0]) / (xs[-1] - xs[0]) * 220
cy = lambda v: 150 - max(min(v, 4), -4) / 4 * 90
for gv, lab in [(3, '+3σ'), (-3, '-3σ')]:
    s += L(426, cy(gv), 652, cy(gv), RED, 1, '4,3') + T(648, cy(gv) + (10 if gv < 0 else -4), lab, 8.5, RED, 'end', 600)
s += L(426, cy(0), 652, cy(0), MUT, 1, '3,2')
# 抽稀到 ~100 点，坐标保留 1 位小数，严格控制 SVG 体积
step_ds = max(1, len(zw) // 100)
pts_ds = [(xs[i], zw[i]) for i in range(0, len(zw), step_ds)]
s += PL([(round(cx(x), 1), round(cy(z), 1)) for x, z in pts_ds], P1, 1.2)
maxz = max(abs(min(zw)), abs(max(zw)))
n_out = sum(1 for z in zw if abs(z) > 3)
s += T(430, 258, f'逐窗口 z 最大 {maxz:.1f}σ，{len(zw)} 窗中 {n_out} 窗越 ±3σ', 9.5, INK, weight=600)
s += T(430, 274, '（1036 窗期望假阳性 ~5 窗；越带即正常波动，无持续单侧漂移）', 9, SUB)
s += T(420, 300, f'2026 惊讶度（log2损失）: MA {st["logloss_2026"]:.3f} vs 训练 {st["logloss_train"]:.3f}（{st["surprise_gap"]:+.3f}）', 9.5, INK, weight=600)
s += T(420, 316, f'EG {se["logloss_2026"]:.3f} vs {se["logloss_train"]:.3f}（{se["surprise_gap"]:+.3f}）——一升一降，无系统性退化', 9.5, SUB)
s += '</svg>'
open('_d_stability.svg', 'w', encoding='utf-8').write(s)

# ================= 图6 长程标度 =================
sc, sce = MA['scaling'], EG['scaling']
s = sv(330) + head('维度六 · 长程标度：振幅长记忆（DFA α≈0.8），收益短程均值回归（VR<1）', '左: DFA 双对数（斜率=α）；右上: 方差比 VR(k)；右下: 振幅自相关衰减')
d1 = np.array(sc['dfa_1m_points']); dd = np.array(sc['dfa_day_points'])
lx = lambda t: 70 + (t - d1[:, 0].min()) / (d1[:, 0].max() - d1[:, 0].min()) * 240
ly = lambda v: 210 - (v - d1[:, 1].min()) / (d1[:, 1].max() - d1[:, 1].min() + 0.4) * 150
s += T(60, 62, '1分钟振幅 DFA', 9.5, P1, weight=600)
s += PL([(lx(t), ly(v)) for t, v in d1], P1, 1.6) + ''.join(f'<circle cx="{lx(t):.1f}" cy="{ly(v):.1f}" r="2.6" fill="{P1}"/>' for t, v in d1)
s += T(60, 236, f'α = {sc["dfa_1m_alpha"]:.2f}（EG {sce["dfa_1m_alpha"]:.2f}）→ 波动率聚集、长记忆', 9.5, INK, weight=600)
s += T(60, 252, f'日振幅 DFA α = {sc["dfa_day_alpha"]:.2f} → 日间持续性更强', 9.5, SUB)
s += T(60, 268, '注: 会话≤120根, 日内 DFA 有效标度 4–24 根', 8.5, MUT)
# 右上 VR
s += T(420, 62, '方差比 VR(k)', 10.5, INK, weight=700)
ks = [int(k) for k in sc['var_ratio'] if sc['var_ratio'][k]]
vx = lambda k: 440 + ks.index(k) * 46
vy = lambda v: 150 - (v - 0.5) / 0.6 * 80
s += L(430, vy(1.0), 640, vy(1.0), RED, 1.2, '4,3') + T(636, vy(1.0) - 4, '随机游走', 8.5, RED, 'end', 600)
s += PL([(vx(k), vy(sc['var_ratio'][str(k)])) for k in ks], P1, 1.8)
s += PL([(vx(k), vy(sce['var_ratio'][str(k)])) for k in ks], RED, 1.4, 0.8)
for k in ks:
    s += T(vx(k), 166, f'{k}', 9, MUT, 'middle') + T(vx(k), vy(sc['var_ratio'][str(k)]) - 6, f'{sc["var_ratio"][str(k)]:.2f}', 8, P1, 'middle', 600)
s += T(420, 186, 'VR<1 且随 k 递减：短程收益均值回归', 9.5, INK, weight=600)
# 右下 ACF
s += T(420, 214, '振幅自相关（同会话内池化）', 10.5, INK, weight=700)
lags = [int(k) for k in sc['amp_acf']]
ax = lambda i: 440 + i * 32
ay = lambda v: 300 - v * 70
for gv in [0, 0.2, 0.4, 0.6]:
    s += L(432, ay(gv), 645, ay(gv), '#E8E6DE', 1) + T(428, ay(gv) + 3, f'{gv:.1f}', 8.5, MUT, 'end')
s += PL([(ax(i), ay(sc['amp_acf'][str(k)])) for i, k in enumerate(lags)], P1, 1.8)
s += PL([(ax(i), ay(sce['amp_acf'][str(k)])) for i, k in enumerate(lags)], RED, 1.4, 0.8)
for i, k in enumerate(lags):
    s += T(ax(i), 314, f'{k}', 8.5, MUT, 'middle')
s += T(420, 330 - 8, '60 根后仍 0.34：波动聚集跨会话延续', 9.5, INK, weight=600)
s += '</svg>'
open('_d_scaling.svg', 'w', encoding='utf-8').write(s)

# ================= 图7 条件结构 =================
c, ce = MA['cond'], EG['cond']
s = sv(340) + head('维度七 · 条件结构：综卦「貌离神合」——静态最远，动态最近', '左: P(下一根振幅5档 | 当前态)，MA；右: 条件分布的 JS 距离（比特，越小越像）')
CA = np.array(c['cond_amp'])
x0, y0, cw, chh = 120, 76, 44, 30
s += T(40, 64, '行=当前态 → 列=下一根振幅档(低→高)', 9.5, SUB)
for j in range(5):
    s += T(x0 + j * cw + cw / 2, y0 - 8, f'档{j+1}', 9, MUT, 'middle')
for i in range(8):
    s += T(x0 - 8, y0 + i * chh + chh / 2 + 3, f'{TRIG[STATES[i]]} {STATES[i]}', 9.5, INK, 'end')
    for j in range(5):
        v = CA[i, j]
        col = P1 if v > 0.22 else (P2 if v > 0.19 else (P3 if v > 0.16 else P4))
        s += B(x0 + j * cw, y0 + i * chh, cw - 2, chh - 2, col) + T(x0 + j * cw + cw / 2 - 1, y0 + i * chh + chh / 2 + 3, f'{v*100:.0f}', 8.5, '#FFFFFF' if v > 0.22 else INK, 'middle')
s += T(40, 336, f'各行集中度最高仅 {max(c["row_max_amp"])*100:.0f}%：给定当前态，下一根振幅仍接近边缘分布（20%）', 9.5, INK, weight=600)
# 右: JS
s += T(430, 76, '条件分布 JS 距离（比特）', 10.5, INK, weight=700)
rows = [('综卦对（影线换边）', c['js_zong_mean'], ce['js_zong_mean'], P1),
        ('错卦对（三爻全反）', c['js_cuo_mean'], ce['js_cuo_mean'], RED)]
for i, (lab, v1, v2, col) in enumerate(rows):
    y = 100 + i * 56
    s += T(424, y + 12, lab, 10, INK, 'end', 600)
    s += B(430, y, v1 / 0.05 * 130, 16, col) + T(430 + v1 / 0.05 * 130 + 5, y + 12, f'{v1:.4f}', 9, INK, weight=600)
    s += B(430, y + 18, v2 / 0.05 * 130, 16, '#E5B8B0' if i else P3) + T(430 + v2 / 0.05 * 130 + 5, y + 30, f'{v2:.4f}', 9, MUT)
s += B(430, 216, 9, 9, P1) + T(443, 224, 'MA', 9, SUB) + B(470, 216, 9, 9, P3) + T(483, 224, 'EG', 9, SUB)
s += T(430, 248, '综卦对条件分布几乎相同（JS 0.001–0.002），', 10, INK, weight=600)
s += T(430, 264, '比错卦对近 30 倍。', 10, INK, weight=600)
s += T(430, 288, '距离矩阵说「综卦=大变动」（3.28 > 均值2.89），', 9.5, RED, weight=600)
s += T(430, 304, '条件动态说「综卦=小变动」——预测问题里，', 9.5, RED, weight=600)
s += T(430, 320, '动态口径才是该信的那个。', 9.5, RED, weight=600)
s += '</svg>'
open('_d_cond.svg', 'w', encoding='utf-8').write(s)

# ================= 图8 尾部相依 =================
t = R['tail']
s = sv(300) + head('维度八 · 尾部相依：甲醇×乙二醇在极端行情里「同生共死」', '左: 联合超越概率 / 独立假设（倍）；右: 95% 尾部超越的领先滞后')
qs = ['90', '95', '99']
x0 = 70
vy = lambda v: 210 - v / 36 * 140
for gv in [0, 9, 18, 27, 36]:
    s += L(60, vy(gv), 300, vy(gv), '#E8E6DE', 1) + T(56, vy(gv) + 3, f'{gv}×', 9, MUT, 'end')
for i, q in enumerate(qs):
    v = t['quantiles'][q]['ratio']
    bx = x0 + i * 66
    s += B(bx, vy(v), 40, 210 - vy(v), P1 if q != '99' else RED) + T(bx + 20, vy(v) - 6, f'{v:.1f}×', 10, INK, 'middle', 600)
    s += T(bx + 20, 228, f'振幅>{q}分位', 9, SUB, 'middle')
s += L(60, vy(1), 300, vy(1), GRN, 1.2, '4,3') + T(296, vy(1) - 4, '独立假设 1×', 8.5, GRN, 'end', 600)
s += T(40, 254, '99% 分位联合概率 0.34% vs 独立 0.01%——极端波动几乎必然共振', 10, INK, weight=600)
s += T(40, 270, '条件概率: P(MA极端|EG极端)=34%（EG 同）', 9.5, SUB)
s += T(40, 288, '→ 两品种不是分散化对象，是同一个风险因子的两个读数', 9.5, RED, weight=600)
# 右: lead-lag
s += T(420, 62, '95% 尾部联合超越（领先滞后 ±5 分钟）', 10.5, INK, weight=700)
lead = t['lead']
ks2 = sorted(int(k) for k in lead)
lx = lambda k: 440 + (k + 5) * 30
ly = lambda v: 170 - (v - 0.012) / 0.012 * 100
s += L(432, ly(0.0211), 648, ly(0.0211), P4, 1, '3,2') + T(644, ly(0.0211) - 4, '同步值', 8.5, MUT, 'end')
s += PL([(lx(k), ly(lead[str(k)])) for k in ks2], P1, 1.8) + ''.join(f'<circle cx="{lx(k):.1f}" cy="{ly(lead[str(k)]):.1f}" r="2.6" fill="{P1}"/>' for k in ks2)
s += f'<circle cx="{lx(0):.1f}" cy="{ly(lead["0"]):.1f}" r="4.5" fill="none" stroke="{RED}" stroke-width="1.6"/>'
for k in ks2:
    s += T(lx(k), 186, f'{k:+d}' if k else '0', 8.5, SUB, 'middle')
s += T(420, 212, f'峰值钉在 k=0（{lead["0"]*100:.2f}%），±5 分钟衰减 30%', 9.5, INK, weight=600)
s += T(420, 228, '与收益互相关结论一致：无分钟级领先结构', 9.5, SUB)
s += T(420, 248, '工程含义：', 10, INK, weight=700)
s += T(420, 264, '① 风控共振探测器（同时亮高波动态即联动减仓）', 9.5, SUB)
s += T(420, 280, '② 不存在「甲醇先动、乙二醇跟」的套利结构', 9.5, SUB)
s += '</svg>'
open('_d_tail.svg', 'w', encoding='utf-8').write(s)
print('8 张维度图已生成')
