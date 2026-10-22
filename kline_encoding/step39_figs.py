# -*- coding: utf-8 -*-
"""第二章报告图表：7 张手写 SVG（浅色主题）。
f0 2026 春季挤仓崩塌复盘（价格+成交量）
f1 15 品种共尾矩阵热力图（按产业链重排）
f2 极端七窗口：高波态占比 + 切换率
f3 共尾双基准（train 标定 vs 窗口自身）
f4 回测毛边缘 vs 现实成本
f5 HMM vs 一阶链 vs 二阶链 vs 边际（12 品种）
f6 Kronos 零样本 vs HMM/链/边际
f7 时间维度入模：模型阶梯 + 分时段 slot 增益
"""
import json
import numpy as np
import pandas as pd

EXT = json.load(open('results_extreme.json'))
SEC = json.load(open('results_sector.json'))
CHN = json.load(open('results_sector_chain.json'))
HMM = json.load(open('results_sector_hmm.json'))
BTD = json.load(open('results_backtest_diag.json'))
KRO = json.load(open('results_kronos.json'))
TD = json.load(open('results_time_dim.json'))

P1, P2, P3, P4 = '#3E3480', '#B0543A', '#A397D2', '#D9B8A8'
RED, GREEN, MUT = '#C4372E', '#2E7D4F', '#888780'
T1, T2, GRID = '#26215C', '#5F5E5A', '#E8E6DE'
ORANGE = '#D97B29'
W = 680


def T(x, y, s, size=10.5, fill=T1, anchor='start', weight='400'):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


def L(x1, y1, x2, y2, c=GRID, w=1, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="{w}"{d}/>'


def R(x, y, w, h, c, op=1.0, rx=3):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" height="{max(h,0):.1f}" rx="{rx}" fill="{c}" fill-opacity="{op}"/>'


def PL(pts, c, w=1.2, dash=None):
    if len(pts) < 2:
        return ''
    p = ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<polyline points="{p}" fill="none" stroke="{c}" stroke-width="{w}"{d}/>'


def head(title, sub, h):
    return (f'<svg viewBox="0 0 {W} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{W}" height="{h}" fill="#FBFAF7"/>'
            + T(40, 26, title, 14, T1, weight='600') + T(40, 44, sub, 10.5, MUT))


def ramp(t):
    """白 -> 深紫 插值, t in [0,1]"""
    t = min(max(t, 0.0), 1.0)
    r = int(255 + (62 - 255) * t)
    g = int(255 + (52 - 255) * t)
    b = int(255 + (128 - 255) * t)
    return f'#{r:02X}{g:02X}{b:02X}'


# ================= f0 2026 挤仓崩塌复盘 =================
def f0():
    import sys
    sys.path.insert(0, '.')
    from ke_common import load
    h = 380
    s = head('2026 春季极端行情复盘：板块级挤仓崩塌（SQUEEZE26）',
             '甲醇 MA（紫）与乙二醇 EG（橙）主力连续 · 2026-01-05 ~ 06-12 · 收盘价归一（首日=100）· 下：日成交量', h)
    dly = {}
    for sym in ['MA', 'EG']:
        d = load(sym, ('2026',))
        g = d.groupby('td').agg(c=('close', 'last'), v=('volume', 'sum'), dt=('datetime', 'last'))
        g = g.sort_values('dt')
        dly[sym] = g
    g0 = dly['MA']
    n = len(g0)
    x0, x1 = 60, 640
    pw = (x1 - x0) / max(n - 1, 1)
    py0, py1 = 70, 210          # 价格 panel
    vy0, vy1 = 250, 330         # 成交量 panel
    allc = np.concatenate([dly[k].c.values for k in dly])
    cmin, cmax = allc.min() * 0.985, allc.max() * 1.01
    def cy(v):
        return py1 - (v - cmin) / (cmax - cmin) * (py1 - py0)
    # 窗口阴影：SQUEEZE26 = 03-01~04-30
    dts = pd.to_datetime(g0.index)
    m = (dts >= '2026-03-01') & (dts <= '2026-04-30')
    if m.any():
        i0, i1 = np.where(m)[0][0], np.where(m)[0][-1]
        s += R(x0 + i0 * pw, py0 - 8, (i1 - i0 + 1) * pw, (vy1 - py0 + 8), ORANGE, 0.10)
        s += T(x0 + i0 * pw, py0 - 14, 'SQUEEZE26 窗口', 9.5, ORANGE, weight='600')
    # y 网格
    for gv in np.linspace(cmin, cmax, 5):
        s += L(x0, cy(gv), x1, cy(gv), GRID)
        s += T(x0 - 6, cy(gv) + 3, f'{gv:.0f}', 9, MUT, 'end')
    for sym, cc in [('MA', P1), ('EG', ORANGE)]:
        g = dly[sym]
        pts = [(x0 + i * pw, cy(v)) for i, v in enumerate(g.c.values)]
        s += PL(pts, cc, 1.6)
    # 04-07 顶 / 04-08 崩 标注
    for sym, cc, dy in [('MA', P1, 0), ('EG', ORANGE, 14)]:
        g = dly[sym]
        dts_s = pd.to_datetime(g.index)
        i_peak = int(np.argmax((dts_s <= '2026-04-07') * g.c.values))
        i_crash = int(np.argmax((dts_s == '2026-04-08') * g.c.values))
        s += f'<circle cx="{x0+i_peak*pw:.1f}" cy="{cy(g.c.values[i_peak]):.1f}" r="3" fill="{cc}"/>'
        s += f'<circle cx="{x0+i_crash*pw:.1f}" cy="{cy(g.c.values[i_crash]):.1f}" r="3" fill="{cc}"/>'
        s += T(x0 + i_peak * pw - 4, cy(g.c.values[i_peak]) - 8, '04-07 顶', 8.5, cc, 'end', '600')
        s += T(x0 + i_crash * pw + 4, cy(g.c.values[i_crash]) + 14, '04-08 崩', 8.5, cc, 'start', '600')
    s += T(x0 + 2, 222, '04-07 双双见顶（MA 3603 / EG 5814）→ 04-08 板块级崩塌：MA 跳空 −1482bp、首根振幅 213 点、', 10, T1, weight='600')
    s += T(x0 + 2, 237, '成交量 15.7 万手（平日 10–30×）而 OI 仅 −1% —— 挤仓崩塌而非基本面重定价；至 06-12 MA −15.8% / EG −19.1%', 9.5, T2)
    # 成交量 panel（MA）
    vmax = dly['MA'].v.max()
    bw = max(pw * 0.7, 1.2)
    for i, v in enumerate(dly['MA'].v.values):
        bh = v / vmax * (vy1 - vy0)
        s += R(x0 + i * pw - bw / 2, vy1 - bh, bw, bh, P3, 0.85, 1)
    s += L(x0, vy1, x1, vy1, GRID)
    s += T(x0 - 6, vy0 + 4, f'{vmax/1e4:.0f}万手', 9, MUT, 'end')
    s += T(x0 - 6, vy1 + 4, '0', 9, MUT, 'end')
    # x 月份刻度
    for mth in ['2026-01-05', '2026-02-16', '2026-04-01', '2026-05-11', '2026-06-12']:
        eqm = (pd.to_datetime(g0.index) == mth)
        if not eqm.any():
            continue
        idx = int(np.argmax(eqm))
        s += L(x0 + idx * pw, py0 - 8, x0 + idx * pw, vy1, GRID)
        s += T(x0 + idx * pw, vy1 + 16, mth[5:7] + '月', 9, MUT, 'middle')
    s += '</svg>'
    return s


# ================= f1 共尾矩阵热力图 =================
ORDER = ['TA', 'PX', 'PF', 'PR', 'PP', 'L', 'V', 'FG', 'SA', 'MA', 'EG', 'UR', 'EB', 'SC', 'IF']
CHAIN_BOX = [('聚酯链 TA-PX-PF-PR', 0, 4), ('聚烯烃 PP-L-V', 4, 7), ('纯碱-玻璃 FG-SA', 7, 9),
             ('甲醇链 MA-EG-UR', 9, 12), ('其他 EB-SC-IF', 12, 15)]


def f1():
    h = 500
    s = head('板块共尾矩阵：分钟桶 slot 调整后的极端振幅共同发生倍数（train 标定基准）',
             '15×15，按产业链重排；格内数字 = 联合超阈概率 / slot 独立期望；>1 即存在 slot 无法解释的共同波动', h)
    cell = 25.5
    ox, oy = 108, 66
    for i, a in enumerate(ORDER):
        s += T(ox - 8, oy + i * cell + cell * 0.72, a, 9.5, T1, 'end', '600')
        s += T(ox + i * cell + cell * 0.4, oy - 8, a, 9.5, T1, 'middle', '600')
    for i, a in enumerate(ORDER):
        for j, b in enumerate(ORDER):
            x, y = ox + j * cell, oy + i * cell
            if a == b:
                s += R(x + 1, y + 1, cell - 2, cell - 2, '#F1EFE7', 1, 2)
                continue
            k = f'{a}-{b}' if f'{a}-{b}' in SEC['cotail'] else f'{b}-{a}'
            v = SEC['cotail'][k]['mult_slot_train']
            t = (np.log(v) - np.log(1.0)) / (np.log(9.5) - np.log(1.0))
            s += R(x + 1, y + 1, cell - 2, cell - 2, ramp(t), 1, 2)
            s += T(x + cell / 2, y + cell * 0.68, f'{v:.1f}', 8, '#FFFFFF' if t > 0.55 else T1, 'middle')
    # 链条边框
    for name, i0, i1 in CHAIN_BOX:
        s += (f'<rect x="{ox+i0*cell-3:.1f}" y="{oy+i0*cell-3:.1f}" width="{(i1-i0)*cell+6:.1f}" '
              f'height="{(i1-i0)*cell+6:.1f}" fill="none" stroke="#B0543A" stroke-width="1.4" rx="4"/>')
        s += T(ox + i0 * cell, oy + i1 * cell + 16, name, 9, '#B0543A', 'start', '600')
    # 图例
    lx, ly = 108, 452
    s += T(lx, ly + 4, '1.0×', 9, MUT, 'end')
    for i in range(60):
        t = i / 59
        s += R(lx + 4 + i * 4.6, ly - 8, 4.6, 12, ramp(t), 1, 1)
    s += T(lx + 4 + 60 * 4.6 + 4, ly + 4, '9.5×', 9, MUT, 'start')
    s += T(lx + 300, ly + 4, '化工两两 mean 2.36×（1.27–6.52）；holdout mean 15.63×（含 2026 挤仓）', 9.5, T2)
    s += T(lx + 300, ly + 18, '最强：PP-L 6.52 · FG-SA 4.25 · MA-EG 4.10；IF 列在 slot 剥离后反而最高（见正文）', 9.5, T2)
    s += '</svg>'
    return s


# ================= f2 极端七窗口 =================
def f2():
    h = 350
    s = head('极端行情专题：七窗口的隐状态体制（MA 实 / EG 虚）',
             '左：HMM 高波态时间占比；右：状态切换率（次/千根）。2020 与 2026 极端窗口 vs 2024 平静基线', h)
    wins = ['CRASH20', 'BOUNCE20', 'BULL21', 'TARIFF25', 'SQUEEZE26', 'CALM24']
    lab = ['2020\n崩盘', '2020\n反弹', '2021\n牛', '2025\n关税', '2026\n挤仓', '2024\n平静']
    # 左 panel：高波态占比
    px0, px1 = 60, 350
    py0, py1 = 78, 250
    bw = (px1 - px0) / len(wins)
    for gv in [0, 25, 50, 75, 100]:
        s += L(px0, py1 - gv / 100 * (py1 - py0), px1, py1 - gv / 100 * (py1 - py0), GRID)
        s += T(px0 - 6, py1 - gv / 100 * (py1 - py0) + 3, f'{gv}%', 9, MUT, 'end')
    for i, w in enumerate(wins):
        for j, (sym, cc, dash) in enumerate([('MA', P1, None), ('EG', ORANGE, '4,2')]):
            v = EXT['windows'][sym][w]['hmm_occ'][2] * 100
            x = px0 + i * bw + 6 + j * (bw - 12) / 2
            bh = v / 100 * (py1 - py0)
            s += R(x, py1 - bh, (bw - 12) / 2 - 2, bh, cc, 0.9 if j == 0 else 0.55, 2)
            s += T(x + (bw - 12) / 4 - 1, py1 - bh - 4, f'{v:.0f}', 8, cc, 'middle', '600')
        s += T(px0 + i * bw + bw / 2, py1 + 14, lab[i], 8.5, T1, 'middle')
    s += T(px0, py1 + 34, '持续型窗口 68–95% vs 跳空型事件（2025 关税）9–23% vs 平静年 5–13%', 10, T1, weight='600')
    # 右 panel：切换率（log 刻度）
    qx0, qx1 = 420, 640
    lo, hi = np.log10(8), np.log10(500)
    for gv in [10, 30, 100, 300]:
        yy = py1 - (np.log10(gv) - lo) / (hi - lo) * (py1 - py0)
        s += L(qx0, yy, qx1, yy, GRID)
        s += T(qx0 - 6, yy + 3, f'{gv}', 9, MUT, 'end')
    for i, w in enumerate(wins):
        for j, (sym, cc) in enumerate([('MA', P1), ('EG', ORANGE)]):
            v = EXT['windows'][sym][w]['hmm_switches_per_1000']
            yy = py1 - (np.log10(max(v, 8)) - lo) / (hi - lo) * (py1 - py0)
            x = qx0 + i * bw + 6 + j * (bw - 12) / 2
            s += R(x, yy, (bw - 12) / 2 - 2, py1 - yy, cc, 0.9 if j == 0 else 0.55, 2)
            s += T(x + (bw - 12) / 4 - 1, yy - 4, f'{v:.0f}', 8, cc, 'middle', '600')
        s += T(qx0 + i * bw + bw / 2, py1 + 14, lab[i], 8.5, T1, 'middle')
    s += T(qx0, py1 + 34, '切换率：MA 持续型窗口 11–53 vs 平静 371；EG 无此模式（44–97）', 10, T1, weight='600')
    s += T(qx0, py1 + 48, '跳空 ≠ 体制：2025 关税是一次 −652bp 跳空 + 平静月末，高波态占比不升', 9.5, T2)
    s += '</svg>'
    return s


# ================= f3 共尾双基准 =================
def f3():
    h = 340
    s = head('共尾的双基准：压力放大（train 标定）vs 结构共尾（窗口自身）',
             '甲醇 MA；同一条共尾事实回答两个不同问题：相对全历史训练基准放大了多少 / 窗口内部结构上共尾多少', h)
    wins = ['CRASH20', 'BOUNCE20', 'BULL21', 'TARIFF25', 'SQUEEZE26', 'CALM24', 'TRAIN']
    lab = ['2020崩盘', '2020反弹', '2021牛', '2025关税', '2026挤仓', '2024平静', '训练集']
    px0, px1 = 60, 640
    py0, py1 = 80, 240
    bw = (px1 - px0) / len(wins)
    vmax = 60.0
    for gv in [0, 15, 30, 45, 60]:
        s += L(px0, py1 - gv / vmax * (py1 - py0), px1, py1 - gv / vmax * (py1 - py0), GRID)
        s += T(px0 - 6, py1 - gv / vmax * (py1 - py0) + 3, f'{gv}×', 9, MUT, 'end')
    for i, w in enumerate(wins):
        c = EXT['cotail'][w]
        v1, v2 = c['mult_slot'], c['mult_win']
        for j, (v, cc, nm) in enumerate([(v1, P1, 'train'), (v2, ORANGE, 'win')]):
            x = px0 + i * bw + 8 + j * (bw - 16) / 2
            bh = min(v, vmax) / vmax * (py1 - py0)
            s += R(x, py1 - bh, (bw - 16) / 2 - 2, bh, cc, 0.9 if j == 0 else 0.6, 2)
            s += T(x + (bw - 16) / 4 - 1, py1 - bh - 4, f'{v:.0f}×' if v >= 10 else f'{v:.1f}×', 8.5, cc, 'middle', '600')
        s += T(px0 + i * bw + bw / 2, py1 + 14, lab[i], 9, T1, 'middle')
    # 图例
    s += R(px0, 262, 12, 10, P1, 0.9) + T(px0 + 18, 271, 'train 标定基准（压力放大倍数）', 9.5, T2)
    s += R(px0 + 210, 262, 12, 10, ORANGE, 0.6) + T(px0 + 228, 271, '窗口自身基准（结构性共尾）', 9.5, T2)
    s += T(px0, 292, '五个极端窗口：压力放大 25.6–57.4×，而结构共尾稳定在 2.4–4.6×；平静年结构共尾仅 0.31×', 10, T1, weight='600')
    s += T(px0, 308, '口径提示：2025-04 的 naive 基准（68.2×）是关税月自身高频事件压扁基准造成的失真，故一律采用 slot 调整基准', 9.5, T2)
    s += '</svg>'
    return s


# ================= f4 回测毛边缘 vs 成本 =================
def f4():
    h = 360
    s = head('可交易性判决：毛边缘 vs 现实成本（clv 极端格点反向策略，h=1）',
             '柱=各品种毛边缘（bp/笔，train）；灰带=现实往返成本区间（佣金 0.5–2‱/边 + 滑点 2.5–10bp/边 → 往返 6–24bp）', h)
    rows = sorted([(sym, BTD[sym]['h1']['gross_q80_bp']) for sym in CHN.keys()], key=lambda r: -r[1])
    px0, px1 = 60, 640
    py0, py1 = 84, 250
    bw = (px1 - px0) / len(rows)
    vmax = 25.0
    for gv in [0, 5, 10, 15, 20, 25]:
        s += L(px0, py1 - gv / vmax * (py1 - py0), px1, py1 - gv / vmax * (py1 - py0), GRID)
        s += T(px0 - 6, py1 - gv / vmax * (py1 - py0) + 3, f'{gv}', 9, MUT, 'end')
    # 成本带 6-24bp（往返）
    y_hi = py1 - 6 / vmax * (py1 - py0)
    y_lo = py1 - 24 / vmax * (py1 - py0)
    s += R(px0, y_lo, px1 - px0, y_hi - y_lo, '#888780', 0.16)
    s += T(px1 - 4, y_hi - 4, '现实往返成本 6–24bp', 9.5, '#6E6E6A', 'end', '600')
    for i, (sym, v) in enumerate(rows):
        x = px0 + i * bw + 5
        bh = v / vmax * (py1 - py0)
        cc = P1 if v >= 2 else (ORANGE if v >= 1.4 else RED)
        s += R(x, py1 - bh, bw - 10, bh, cc, 0.88, 2)
        s += T(x + (bw - 10) / 2, py1 - bh - 4, f'{v:.1f}', 8.5, cc, 'middle', '600')
        s += T(x + (bw - 10) / 2, py1 + 14, sym, 9, T1, 'middle')
    s += T(px0, py1 + 34, '池化毛边缘 +1.46bp/笔（n=317.9 万，t=329.7）→ 盈亏平衡往返成本 1.46bp = 每边 0.73bp', 10, T1, weight='600')
    s += T(px0, py1 + 50, '现实每边 ~6bp：差 8 倍。15 品种全部净收益 ≈ −10bp/笔 ≈ 成本本身；毛边缘不随持有期（1–60 分钟）变化', 9.5, T2)
    s += T(px0, py1 + 66, '结论：1 分钟 clv 边缘是微观结构签名（收在极值⟺下一根回拉），量级 ≈ 半个价差，实盘不可交易', 9.5, T2)
    s += '</svg>'
    return s


# ================= f5 HMM vs 链 vs 边际 =================
def f5():
    h = 360
    s = head('预测模型横评：HMM vs 一阶链 vs 二阶链 vs 边际（holdout log-loss，bit/根，越低越好）',
             '15 品种（化工 12 + SC + IF）；链仅用自身 8 态转移，HMM 用连续振幅发射 + 隐状态；灰虚线=边际（≈log2 5=2.32 均匀基线）', h)
    syms = sorted(CHN.keys(), key=lambda k: HMM[k]['hmm_nll'])
    px0, px1 = 60, 640
    py0, py1 = 80, 240
    bw = (px1 - px0) / len(syms)
    vmax = 2.5
    for gv in [1.0, 1.5, 2.0, 2.5]:
        s += L(px0, py1 - gv / vmax * (py1 - py0), px1, py1 - gv / vmax * (py1 - py0), GRID)
        s += T(px0 - 6, py1 - gv / vmax * (py1 - py0) + 3, f'{gv:.1f}', 9, MUT, 'end')
    for i, sym in enumerate(syms):
        vals = [HMM[sym]['hmm_nll'], CHN[sym]['m1'], CHN[sym]['m2'], CHN[sym]['marg']]
        ccs = [P1, P3, P4, '#C9C7BE']
        for j, (v, cc) in enumerate(zip(vals, ccs)):
            x = px0 + i * bw + 4 + j * (bw - 8) / 4
            bh = v / vmax * (py1 - py0)
            s += R(x, py1 - bh, (bw - 8) / 4 - 1.5, bh, cc, 0.92, 1.5)
        s += T(px0 + i * bw + bw / 2, py1 - vals[0] / vmax * (py1 - py0) - 4, f'{vals[0]:.2f}', 8.5, P1, 'middle', '600')
        s += T(px0 + i * bw + bw / 2, py1 + 14, sym, 9, T1, 'middle')
    # 均匀基线
    s += L(px0, py1 - 2.322 / vmax * (py1 - py0), px1, py1 - 2.322 / vmax * (py1 - py0), '#888780', 1, '4,3')
    s += T(px1 - 4, py1 - 2.322 / vmax * (py1 - py0) - 4, '均匀基线 2.322', 9, '#6E6E6A', 'end')
    s += R(px0, 258, 11, 9, P1, 0.92) + T(px0 + 16, 267, 'HMM(K=3)', 9.5, T2)
    s += R(px0 + 90, 258, 11, 9, P3, 0.92) + T(px0 + 106, 267, '一阶链', 9.5, T2)
    s += R(px0 + 160, 258, 11, 9, P4, 0.92) + T(px0 + 176, 267, '二阶链', 9.5, T2)
    s += R(px0 + 230, 258, 11, 9, '#C9C7BE', 0.92) + T(px0 + 246, 267, '边际', 9.5, T2)
    s += T(px0, 292, '14/15 品种 HMM 大胜（增益 0.14–0.83 bit，中位 0.54）；链 12/15 有小增益（V/IF 可忽略）、L/FG/SA 输给边际', 10, T1, weight='600')
    s += T(px0, 308, '唯一输家 UR（2.335 vs 链 2.261）：2026 无夜盘 → holdout 类别分布下移；10 重启重拟合确认非局部最优（logL −0.7112 收敛同值）', 9.5, T2)
    s += T(px0, 324, 'FG top-1 失真（0.177）：挤仓年 train 分位点外推 → class-2 整箱空；nll 口径比较仍有效', 9.5, T2)
    s += '</svg>'
    return s


# ================= f6 Kronos vs HMM =================
def f6():
    h = 330
    s = head('Kronos 基础模型零样本基准：同一任务（下一根振幅五档 · holdout）',
             '上下文 512 根、每点 20 样本 T=1.0/top_p=0.95；与轻量模型同口径 log-loss + top-1', h)
    groups = []
    for sym in ['MA', 'EG']:
        if sym not in KRO:
            continue
        groups.append((sym, KRO[sym]['nll'], KRO[sym]['nll_se'], KRO[sym]['top1']))
    if not groups:
        s += T(60, 120, '（Kronos 结果尚未生成）', 12, MUT)
        s += '</svg>'
        return s
    px0, px1 = 60, 400
    py0, py1 = 84, 240
    bw = (px1 - px0) / len(groups)
    vmax = 4.5
    for gv in [1, 2, 3, 4]:
        s += L(px0, py1 - gv / vmax * (py1 - py0), px1, py1 - gv / vmax * (py1 - py0), GRID)
        s += T(px0 - 6, py1 - gv / vmax * (py1 - py0) + 3, f'{gv:.1f}', 9, MUT, 'end')
    for i, (sym, nll, se, top1) in enumerate(groups):
        ref = [('Kronos', nll, se, '#B0543A'), ('HMM', HMM[sym]['hmm_nll'], None, P1),
               ('一阶链', CHN[sym]['m1'], None, P3), ('边际', CHN[sym]['marg'], None, '#C9C7BE')]
        gw = (bw - 20) / 4
        for j, (nm, v, e, cc) in enumerate(ref):
            x = px0 + i * bw + 10 + j * gw
            bh = v / vmax * (py1 - py0)
            s += R(x, py1 - bh, gw - 3, bh, cc, 0.9, 2)
            if e:
                ye = py1 - (v + e) / vmax * (py1 - py0)
                s += L(x + (gw - 3) / 2, ye, x + (gw - 3) / 2, py1 - (v - e) / vmax * (py1 - py0), '#26215C', 1.2)
            s += T(x + (gw - 3) / 2, py1 - bh - 4, f'{v:.2f}', 8.5, cc, 'middle', '600')
            s += T(x + (gw - 3) / 2, py1 + 13, nm, 8, T2, 'middle')
        s += T(px0 + i * bw + bw / 2, py1 + 28, sym, 10, T1, 'middle', '700')
    # 右 panel：top-1
    qx0, qx1 = 450, 640
    for gv in [0, 0.25, 0.5, 0.75, 1.0]:
        s += L(qx0, py1 - gv * (py1 - py0), qx1, py1 - gv * (py1 - py0), GRID)
        s += T(qx0 - 6, py1 - gv * (py1 - py0) + 3, f'{gv:.2f}', 9, MUT, 'end')
    for i, (sym, nll, se, top1) in enumerate(groups):
        ref = [('Kronos', top1, '#B0543A'), ('HMM', HMM[sym]['hmm_top1'], P1)]
        gw = (bw - 20) / 2
        for j, (nm, v, cc) in enumerate(ref):
            x = qx0 + i * bw + 10 + j * gw
            bh = v * (py1 - py0)
            s += R(x, py1 - bh, gw - 3, bh, cc, 0.9, 2)
            s += T(x + (gw - 3) / 2, py1 - bh - 4, f'{v:.2f}', 8.5, cc, 'middle', '600')
            s += T(x + (gw - 3) / 2, py1 + 13, nm, 8, T2, 'middle')
    s += T(qx0, py1 + 28, 'top-1 准确率（随机基线 0.20）', 9.5, T2)
    s += T(px0, 292, 'Kronos 零样本 nll：MA 4.05±0.17 / EG 4.31±0.16（N=200，均匀基线 2.32 的 ~1.8 倍劣化）vs HMM 1.77 / 1.68', 10, T1, weight='600')
    s += T(px0, 308, '解读：两级残差量化 token + 自回归采样在 512 上下文里对「下一分钟振幅」过平滑；EG 的 4000 个预测点几乎全部坍缩到最低档，', 9.5, T2)
    s += T(px0, 324, 'MA 也系统性偏移。局限：零样本、CPU、20 样本/点；不否定 Kronos 在长周期形态任务上的能力', 9.5, T2)
    s += '</svg>'
    return s


def f7():
    h = 330
    s = head('时间维度入模：slot 作为显式条件值多少 bit？',
             '左：下一根振幅五档阶梯模型（holdout nll，条件一律取 t-1）；右：slot 单条件相对边际的增益（分时段）', h)
    syms = ['MA', 'EG', 'TA']
    models = [('marg', '边际'), ('slot', '+slot'), ('chain1', '前状态链'), ('chain1_slot', '链×slot'),
              ('vol1', '前波幅'), ('chain1_vol1', '链×前波幅'), ('full', '全件(+slot)')]
    px0, px1 = 60, 385
    py0, py1 = 86, 244
    lo, hi = 1.80, 2.42
    for gv in [2.0, 2.2, 2.4]:
        yy = py1 - (gv - lo) / (hi - lo) * (py1 - py0)
        s += L(px0, yy, px1, yy, GRID)
        s += T(px0 - 6, yy + 3, f'{gv:.1f}', 9, MUT, 'end')
    s += T(px0, py0 - 14, '幅度任务 · holdout nll（越低越好，均匀基线 2.32）', 10, T1, weight='600')
    bw = (px1 - px0) / len(models)
    gw = (bw - 14) / 3
    for i, (mk, nm) in enumerate(models):
        for j, sym in enumerate(syms):
            v = TD[sym]['amp'][mk][1]
            cc = [P1, ORANGE, P3][j]
            x = px0 + i * bw + 7 + j * gw
            bh = (v - lo) / (hi - lo) * (py1 - py0)
            s += R(x, py1 - bh, gw - 3, bh, cc, 0.92, 2)
            s += T(x + (gw - 3) / 2, py1 - bh - 4, f'{v:.2f}', 7.5, cc, 'middle', '600')
        lbl = nm.replace('(', '\n(')
        s += T(px0 + i * bw + bw / 2, py1 + 14, nm.split('(')[0], 8, T2, 'middle')
        if '(' in nm:
            s += T(px0 + i * bw + bw / 2, py1 + 24, '(' + nm.split('(')[1], 7, T2, 'middle')
    # 右 panel：slot 增益分块
    qx0, qx1 = 452, 655
    zy = 165
    gmax = 0.85
    s += L(qx0, zy, qx1, zy, '#B9B6AC')
    nblk = 8
    qw = (qx1 - qx0) / nblk
    blocks = TD['MA']['diag']['slot_gain_map']
    short = ['开盘', '09:15', '10:30', '下午', '收盘', '夜开', '夜中', '夜收']
    for i, b in enumerate(blocks):
        for j, sym in enumerate(syms):
            g = TD[sym]['diag']['slot_gain_map'][i]['gain_bits']
            cc = [P1, ORANGE, P3][j]
            bh = abs(g) / gmax * 70
            x = qx0 + i * qw + 4 + j * (qw - 8) / 3
            if g >= 0:
                s += R(x, zy - bh, (qw - 8) / 3 - 2, bh, cc, 0.92, 2)
                if j < 2 and g > 0.1:
                    s += T(x + ((qw - 8) / 3 - 2) / 2, zy - bh - 3, f'+{g:.2f}', 7, cc, 'middle', '700')
            else:
                s += R(x, zy, (qw - 8) / 3 - 2, bh, '#B9B6AC', 0.55, 2)
        s += T(qx0 + i * qw + qw / 2, zy + 12, short[i], 8.5, T2, 'middle')
    s += T(qx0, py0 - 14, 'slot 单条件增益（holdout，正=有时间信息）', 10, T1, weight='600')
    s += T(qx0, zy + 26, '开盘两块 +0.49~+0.76 bit，其余 ≤0：时间信息几乎全部集中在两个开盘 15 分钟', 9.5, T1, weight='600')
    s += T(px0, 292, '结论：slot 单独 +0.02~0.04 bit/根（3 品种一致）；叠到「前波幅+链」之上只剩 +0.003~0.005 bit——', 10, T1, weight='600')
    s += T(px0, 308, '时间维度与振幅可预测性高度共线。方向任务 slot 增益 ≈ 0（MA 0.9793→0.9792），方向信息载体是 clv', 9.5, T2)
    s += T(px0, 324, '链转移矩阵在开盘块偏离全池最多（TV 0.105 vs 盘中 0.015-0.035）——时间非齐次性存在，但只发生在开盘', 9.5, T2)
    s += '</svg>'
    return s


if __name__ == '__main__':
    figs = {'f0': f0(), 'f1': f1(), 'f2': f2(), 'f3': f3(), 'f4': f4(), 'f5': f5(), 'f6': f6(), 'f7': f7()}
    import os
    os.makedirs('figs', exist_ok=True)
    for k, v in figs.items():
        open(f'figs/{k}.svg', 'w', encoding='utf-8').write(v)
        print(k, len(v))
