# -*- coding: utf-8 -*-
"""深挖三维度图表：5 张手写 SVG（浅色主题，折线≤100 点）。
f1 微观结构（日内U型 + OFI五档） f2 持续期（hazard + run-length 分布）
f3 HMM vs 显态（holdout log-loss + 隐状态结构） f4 共尾（原始vs调整 + 领先滞后）
f5 滚动共尾时间序列
"""
import json
import numpy as np

MICRO = json.load(open('results_micro.json'))
PERS = json.load(open('results_persist.json'))
CROSS = json.load(open('results_cross.json'))

P1, P2, P3, P4 = '#3E3480', '#6E5FB8', '#A397D2', '#CFC6E6'
RED, GREEN, MUT = '#C4372E', '#2E7D4F', '#888780'
T1, T2, GRID = '#26215C', '#5F5E5A', '#E8E6DE'
W, H = 680, 330


def T(x, y, s, size=10.5, fill=T1, anchor='start', weight='400'):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


def L(x1, y1, x2, y2, c=GRID, w=1, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="{w}"{d}/>'


def R(x, y, w, h, c, op=1.0, rx=3):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" height="{max(h,0):.1f}" rx="{rx}" fill="{c}" fill-opacity="{op}"/>'


def PL(pts, c, w=1.2, dash=None):
    if len(pts) < 2: return ''
    p = ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<polyline points="{p}" fill="none" stroke="{c}" stroke-width="{w}"{d}/>'


def head(title, sub):
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{W}" height="{H}" fill="#FBFAF7"/>'
            + T(40, 26, title, 14, T1, weight='600') + T(40, 44, sub, 10.5, MUT))


# ================= f1 微观结构 =================
s = head('维度九 · 微观结构：开盘定乾坤、订单流代理预测回归',
         '左: 日内 |收益| 与成交量轮廓（日盘, MA/EG 平均）; 右: clv=(C-L)/(H-L) 五档 → 下一根收益')
def nn(xs):
    """过滤 None"""
    return [x for x in xs if x is not None]


for i, (sym, cc) in enumerate([('MA', P1), ('EG', P2)]):
    prof = nn(MICRO[sym]['intraday']['prof_absr_bp'][:135])
    xs = list(range(len(prof)))
    step = max(1, len(xs) // 45)
    pts = [(60 + xs[i] * 1.9, 235 - min(prof[i], 12) / 12 * 160) for i in range(0, 135, step) if i < len(prof)]
    s += PL(pts, cc, 1.4)
    v = nn(MICRO[sym]['intraday']['vprof'][:135])
    vmax = max(v)
    pts2 = [(60 + i * 1.9, 235 - v[i] / vmax * 150) for i in range(0, 135, step) if i < len(v)]
    s += PL(pts2, P3 if sym == 'MA' else P4, 1.1, '3,2')
for gx, lab in [(60, '09:00'), (135, '10:00'), (210, '11:00'), (285, '14:00'), (345, '15:00')]:
    s += L(gx * 1.9 + 0, 75, gx * 1.9, 235, GRID) + T(gx * 1.9, 250, lab, 9, MUT, 'middle')
s += L(60, 235, 345, 235, GRID)
s += T(60, 300, '开盘 15 分钟集中了当日 63% 的振幅、52% 的成交量（MA/EG 一致）', 10, T1, weight='600')
s += T(60, 316, '实线=|close-to-close| 虚线=成交量（各自归一）; U 型左尾=开盘尖峰', 9.5, T2)
# 右panel: clv 五档
qx = [420, 480, 540, 600, 652]
for i, sym in enumerate(['MA', 'EG']):
    qs = MICRO[sym]['ofi']['quintiles']
    vals = [q['next_rc_bp'] for q in qs]
    fin = [v for v in vals if v is not None and v == v]          # 剔平局空箱（MA 的 Q1/Q3 为 NaN）
    vmax = max(abs(min(fin)), abs(max(fin)))
    for j, v in enumerate(vals):
        if v is None or v != v:
            continue                                              # 空箱不画柱
        h = abs(v) / vmax * 60
        x0 = qx[j] + (0 if sym == 'MA' else 12)
        col = RED if v < 0 else GREEN
        s += R(x0, 150 - (h if v < 0 else 0), 10, h, col, 0.85 if sym == 'MA' else 0.45)
    note = '' if len(fin) == 5 else '（平局致五档退化三档）'
    s += T(430, 105 + i * 14, f'{sym}: 下一根收益 {fin[0]:+.2f}→{fin[-1]:+.2f} bp{note}', 9, P1 if sym == 'MA' else P2)
s += L(420, 150, 652, 150, MUT, 1)
s += T(420, 70, 'clv 越高（收在最高）→ 下一根越跌', 10.5, T1, weight='600')
s += T(420, 86, 'IC(train→holdout): MA -0.198→-0.119 | EG -0.103→-0.067', 9, T2)
for j, q in enumerate(range(1, 6)):
    s += T(qx[j] + 8, 168, f'Q{q}', 9, MUT, 'middle')
s += T(420, 200, '负 IC = 均值回归（买卖价差反弹）', 10, T1, weight='600')
s += T(420, 216, '累计 5 根 OFI → 未来 5 根收益 IC +0.11~+0.14（弱持续）', 9.5, T2)
s += T(420, 232, 'vol→下一根振幅 IC 0.46→0.63（量领波动）', 9.5, T2)
s += T(420, 248, 'vol→下一根收益 IC ≈ 0（量不指示方向）', 9.5, T2)
s += T(420, 280, '⚠ 推翻 D4「周一振幅 0.69×」：数据源周日无交易 →', 10, RED, weight='600')
s += T(420, 296, '周一 td 缺夜盘，D4 的 69 块里 24 块被 0 填充（45/69=0.65 稀释）', 9.5, RED)
s += T(420, 312, '真相：周一（纯日盘）1min 振幅五天最高，开盘首 5 分钟块 |收益| 比 1.68×', 9.5, RED)
s += '</svg>'
open('_dd_micro.svg', 'w', encoding='utf-8').write(s)

# ================= f2 持续期 =================
s = head('维度十 · 状态持续期：一阶马尔可夫抓得住头部，抓不住长尾',
         '左: run 结束风险函数 hazard(k)（删失修正）; 右: run 长度分布 vs 几何基准')
hz_x = lambda k: 60 + (k - 1) / 11 * 300
hz_y = lambda h: 235 - h / 0.9 * 170
for hv in [0.2, 0.4, 0.6, 0.8]:
    s += L(60, hz_y(hv), 360, hz_y(hv), GRID) + T(54, hz_y(hv) + 3, f'{hv:.1f}', 9, MUT, 'end')
s += L(60, hz_y(0.858), 360, hz_y(0.858), RED, 1.2, '4,3') + T(362, hz_y(0.858) + 3, '几何基准 0.858', 8.5, RED)
for sym, cc in [('MA', P1), ('EG', P2)]:
    hz = PERS[sym]['hazard_fix']['hazard_censor']
    pts = [(hz_x(k + 1), hz_y(min(h, 0.9))) for k, h in enumerate(hz[:11])]
    s += PL(pts, cc, 1.5)
s += T(60, 258, 'hazard 递减：持续越久越不容易结束（热手/重尾）', 10, T1, weight='600')
s += T(60, 274, 'k=1 时 0.83~0.84≈基准；k=8 时降到 0.15~0.42', 9.5, T2)
for k in [1, 3, 5, 7, 9, 11]:
    s += T(hz_x(k), 250, str(k), 9, MUT, 'middle')
# 右: run 长度分布
bx = lambda k: 430 + (k - 1) * 36
for i, (sym, cc) in enumerate([('MA', P1), ('EG', P2)]):
    hf = PERS[sym]['hazard_fix']
    obs, mk = hf['obs_dist'], hf['markov_dist']
    for k in range(1, 7):
        h1 = min(obs[k - 1], 0.9) / 0.9 * 150
        h2 = min(mk[k - 1], 0.9) / 0.9 * 150
        s += R(bx(k) + i * 14, 235 - h1, 12, h1, P1 if sym == 'MA' else P2, 0.85)
        s += R(bx(k) + i * 14, 235 - h2, 12, h2, RED, 0.35)
    s += T(430, 105 + i * 14, f'{sym}: P(L≥5) 实际 {hf["long_run_share"]*100:.2f}% vs 几何 {hf["long_run_share_markov"]*100:.3f}%',
           9, P1 if sym == 'MA' else P2)
for k in range(1, 7):
    s += T(bx(k) + 14, 250, str(k), 9, MUT, 'middle')
s += L(430, 235, 650, 235, GRID)
s += T(430, 70, 'run 长度分布（实=实际, 淡红=几何基准）', 10.5, T1, weight='600')
s += T(430, 280, '头部（L=1）一致：85% vs 86%', 10, T1, weight='600')
s += T(430, 296, '长尾差 7~9 倍：状态有「 regime 」成分', 9.5, T2)
s += T(430, 312, '平均 run 长 1.0~1.35 根：总体仍是浅记忆', 9.5, T2)
s += '</svg>'
open('_dd_persist.svg', 'w', encoding='utf-8').write(s)

# ================= f3 HMM =================
ev3 = PERS['MA']['eval']['3']; ev3e = PERS['EG']['eval']['3']
s = head('维度十一 · 3 状态波动体制 HMM 大幅打败 8 态八卦链',
         '左: holdout 2026 下一根振幅五档 log2/bar（越低越好）; 右: K=3 隐状态结构')
models = [('边际', 'marg_ll', MUT), ('一阶链', 'm1_ll', P3), ('二阶链', 'm2_ll', P2), ('HMM K=3', 'hmm_ll', RED)]
bx = lambda i: 70 + i * 72
vmax, vmin = 2.45, 1.50
for i, (nm, key, cc) in enumerate(models):
    for j, sym in enumerate(['MA', 'EG']):
        v = PERS[sym]['eval']['3'][key]
        h = (vmax - v) / (vmax - vmin) * 165 if v == v else 0
        x0 = bx(i) + j * 26
        s += R(x0, 235 - h, 22, h, cc, 0.9 if j == 0 else 0.45)
        if v == v:
            s += T(x0 + 11, 228 - h, f'{v:.3f}', 8.5, T1, 'middle')
        else:
            s += T(x0 + 11, 228, 'NaN', 8.5, RED, 'middle')
    s += T(bx(i) + 26, 250, nm, 9, T2, 'middle')
s += L(60, 235, 400, 235, GRID)
s += T(60, 70, 'HMM（对数振幅高斯发射, K=3）比一阶链低 0.42~0.67 bit/bar', 10.5, RED, weight='600')
s += T(60, 86, 'forward 算法通过 3^6 路径暴力枚举校验; EM 全程单调', 9.5, T2)
s += T(60, 280, 'top-1: HMM 0.49/0.53 vs 链 0.30~0.32', 10, T1, weight='600')
s += T(60, 296, '八卦在隐状态之上增量仅 0.012/0.0002 bit/bar ≈ 0', 9.5, T2)
s += T(60, 312, '→ 六位编码对预测问题统计冗余', 9.5, T2)
# 右: 隐状态结构
hid = PERS['MA']['hidden']
inc = PERS['MA']['increment']
mu = hid['mu_log']
mx = lambda m: 460 + (m - mu[0]) / max(mu[-1] - mu[0], 0.5) * 160
for k in range(3):
    y0 = 90 + k * 46
    s += T(mx(mu[k]) - 32, y0 + 4, f'状态{k+1}', 9.5, T1, 'end', '600')
    s += R(mx(mu[k]) - 26, y0 - 8, 52, 16, [P1, P2, P3][k], 0.85)
    s += T(mx(mu[k]), y0 + 4, f'μ={np.exp(mu[k])*100:.3f}%', 8.5, '#FFFFFF', 'middle', '600')
s += L(460, 230, 640, 230, GRID)
s += T(460, 248, '隐状态 = 波动体制（低/中/高波）', 10, T1, weight='600')
s += T(460, 264, f'平均持续 MA {[round(x,1) for x in hid["mean_run"] if x]} 根', 9.5, T2)
s += T(460, 280, f'vs 八卦态持续 1.0~1.35 根', 9.5, T2)
s += T(460, 296, f'隐态×显态互信息仅 {hid["mi_with_explicit_bits"]:.3f} bit', 9.5, T2)
s += T(460, 312, 'EG 低波体制持续 51 根：体制是慢变量', 9.5, T2)
s += '</svg>'
open('_dd_hmm.svg', 'w', encoding='utf-8').write(s)

# ================= f4 共尾 =================
s = head('维度十二 · MA×EG 共尾：34× 里大部分是「同一时钟」的机械共振',
         '左: 99% 分位共尾倍数（原始 vs slot 调整）; 右: 分钟级领先滞后')
years = [str(y) for y in range(2020, 2027)]
bx = lambda i: 70 + i * 40
vmax = 140
for i, y in enumerate(years):
    raw = CROSS['by_year'][y]['0.99']['multiplier']
    adj = CROSS['slot_adj_by_year'][y]['multiplier']
    h1 = min(raw, vmax) / vmax * 150
    h2 = min(adj, vmax) / vmax * 150
    s += R(bx(i), 235 - h1, 16, h1, P1, 0.85)
    s += R(bx(i) + 17, 235 - h2, 16, h2, GREEN, 0.8)
    s += T(bx(i) + 8, 250, y[2:], 9, MUT, 'middle')
    if raw > 20:
        s += T(bx(i) + 8, 230 - h1, f'{raw:.0f}', 8, P1, 'middle')
    s += T(bx(i) + 25, 230 - h2, f'{adj:.1f}', 8, GREEN, 'middle')
s += L(60, 235, 400, 235, GRID)
s += T(60, 70, '原始倍数 12~133× 剧烈摆动 → slot 调整后 1.7~3.7× 稳定', 10.5, T1, weight='600')
s += T(60, 86, 'train 31.4× → 调整 4.1×; holdout 11.5× → 调整 3.5×', 9.5, T2)
s += R(300, 100, 12, 12, P1, 0.85) + T(316, 110, '原始', 9, T2)
s += R(300, 118, 12, 12, GREEN, 0.8) + T(316, 128, 'slot调整', 9, T2)
s += T(60, 280, '摆动来自共享波动体制（2024/25 高、2021 低）', 10, T1, weight='600')
s += T(60, 296, 'amp 相关性 0.38~0.72，2026 最高 0.715', 9.5, T2)
s += T(60, 312, '调整后同向 6.3~6.6× vs 反向 1.5~1.7×：共振是同向的', 9.5, T2)
# 右: 领先滞后
ll = CROSS['leadlag']
lx = lambda k: 480 + (k + 15) * 5.7
ly = lambda m: 235 - (m - 5) / 30 * 150
for gx in [-15, -10, -5, 0, 5, 10, 15]:
    s += L(lx(gx), 85, lx(gx), 235, GRID) + T(lx(gx), 250, str(gx), 9, MUT, 'middle')
ks = sorted(int(k) for k in ll)
pts = [(lx(k), ly(min(ll[str(k)], 35))) for k in ks]
s += PL(pts, P1, 1.4)
s += L(lx(0), 85, lx(0), 235, RED, 1.2, '4,3')
s += T(480, 78, 'k=0 同步 31×; ±1 分钟 17×/16× 对称', 9, RED)
s += T(430, 70, '领先滞后（分钟）', 10.5, T1, weight='600')
s += T(430, 280, '无分钟级领先可套利', 10, T1, weight='600')
s += T(430, 296, '组合 1% 分位 = 边际加总的 0.85×', 9.5, T2)
s += T(430, 312, '（独立性 0.71×）→ 轻度正相依', 9.5, T2)
s += '</svg>'
open('_dd_cross.svg', 'w', encoding='utf-8').write(s)

# ================= f5 滚动共尾 =================
s = head('维度十二 · 滚动共尾倍数：真实但不稳定的风控共振',
         '60 交易日滚动窗口, 99% 分位, slot 调整前后对比（MA×EG, 2020-2026）')
rs = CROSS['roll_series']
step = max(1, len(rs) // 100)
pts = [(60 + i / (len(rs) // step) * 580, 240 - min(v, 140) / 140 * 160) for i, (_, v) in enumerate(rs[::step])]
s += PL(pts, P1, 1.2)
mean_v = CROSS['roll']['mean']
y_mean = 240 - min(mean_v, 140) / 140 * 160
s += L(60, y_mean, 640, y_mean, RED, 1.2, '4,3')
s += T(640, y_mean - 5, f'均值 {mean_v:.1f}×', 9, RED, 'end')
for gy in [0, 35, 70, 105, 140]:
    s += L(60, 240 - gy / 140 * 160, 640, 240 - gy / 140 * 160, GRID) + T(54, 240 - gy / 140 * 160 + 3, str(gy), 9, MUT, 'end')
s += T(60, 280, f'滚动倍数 mean={CROSS["roll"]["mean"]:.1f} sd={CROSS["roll"]["sd"]:.1f} 极值 {CROSS["roll"]["min"]:.0f}~{CROSS["roll"]["max"]:.0f}×', 10, T1, weight='600')
s += T(60, 296, '共尾是「体制变量」不是常数：风控阈值应按滚动窗口动态调整', 9.5, T2)
s += T(60, 312, '2024-25 体制性高共振（化工板块宏观驱动）; 探测器语义=同步警报', 9.5, T2)
s += '</svg>'
open('_dd_roll.svg', 'w', encoding='utf-8').write(s)

import os
for f in ['_dd_micro.svg', '_dd_persist.svg', '_dd_hmm.svg', '_dd_cross.svg', '_dd_roll.svg']:
    print(f, os.path.getsize(f), 'bytes')
