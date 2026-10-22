# -*- coding: utf-8 -*-
"""step44 融合策略审查 + 融合回测报告（fusion_review_report.html）

内容：
  一、程序化交易疏漏审查（V3.4 引擎逐行核对 + 回测证据）
  二、融合点回测对比（BASE / A1 / A1S / A2 / B1 / B2 / B3 / COMBO / COMBO2）
  三、结论与落地建议
图：g1 净值曲线（BASE vs COMBO2，全池+化工池）；g2 变体净利/回撤条形；g3 高波日入场归因
"""
import json
import numpy as np
import pandas as pd

R = json.load(open('results_fusion_bt.json'))
BASE = pd.read_csv('trades_BASE.csv', parse_dates=['edt', 'xdt'])
C2 = pd.read_csv('trades_COMBO2.csv', parse_dates=['edt', 'xdt'])

# ---------- SVG 工具 ----------
INK = '#22251F'; MUT = '#8A877E'; GRID = '#E8E4DA'; PANEL = '#F7F5F0'
RED = '#C0392B'; GREEN = '#1E8449'; BLUE = '#2166AC'; ORANGE = '#D68910'; PURPLE = '#7D3C98'


def head(title, sub, h=64):
    return (f'<rect width="680" height="{h}" fill="{PANEL}" stroke="{GRID}"/>'
            f'<text x="16" y="26" font-size="14" font-weight="700" fill="{INK}">{title}</text>'
            f'<text x="16" y="46" font-size="10.5" fill="{MUT}">{sub}</text>')


def T(x, y, s, sz=10, fill=INK, anchor='start', weight='400'):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{sz}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


def L(x1, y1, x2, y2, c=GRID, w=1):
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="{w}"/>'


def g1():
    """净值曲线：全池 BASE vs COMBO2；化工池（剔IF/SC）BASE vs COMBO2。"""
    def eq(df, excl=()):
        d = df[~df.sym.isin(excl)].sort_values('xdt')
        return d.xdt.values, d.net.cumsum().values / 1e4
    series = [('全池 BASE', BASE, (), '#B9B6AC'),
              ('全池 COMBO2', C2, (), BLUE),
              ('化工池 BASE', BASE, ('IF', 'SC'), '#E0A0A0'),
              ('化工池 COMBO2', C2, ('IF', 'SC'), RED)]
    allx = np.concatenate([eq(df, ex)[0] for df, ex in [(BASE, ()), (C2, ())]])
    x0, x1 = 60, 660
    t0 = np.datetime64('2020-01-01'); t1 = np.datetime64('2026-07-01')
    def tx(t):
        return x0 + (t - t0) / (t1 - t0) * (x1 - x0)
    ys = np.concatenate([eq(df, ex)[1] for _, df, ex, _ in series])
    lo, hi = ys.min(), ys.max()
    y0, y1 = 330, 80
    def cy(v):
        return y1 + (hi - v) / (hi - lo) * (y0 - y1)
    s = head('净值曲线：融合策略 V3.4 基线 vs 融合增强版 COMBO2（万元，按平仓时间累计）',
             '15 品种 1 手/信号 · 2020-01~2026-06 · 成本=1.5bp+1跳/边 · COMBO2=A1S高波日不开仓+A2簇上限+B2开盘根+B3尾盘根')
    for yr in range(2020, 2027):
        x = tx(np.datetime64(f'{yr}-01-01'))
        s += L(x, y1, x, y0) + T(x, y0 + 14, str(yr), 9, MUT, 'middle')
    for gv in np.arange(np.floor(lo / 20) * 20, hi + 20, 20):
        s += L(x0, cy(gv), x1, cy(gv)) + T(x0 - 6, cy(gv) + 3, f'{gv:.0f}', 9, MUT, 'end')
    s += L(x0, cy(0), x1, cy(0), '#B9B6AC')
    for name, df, ex, cc in series:
        xs, yv = eq(df, ex)
        pts = ' '.join(f'{tx(t):.1f},{cy(v):.1f}' for t, v in zip(xs, yv))
        s += f'<polyline points="{pts}" fill="none" stroke="{cc}" stroke-width="1.8"/>'
    for i, (name, _, _, cc) in enumerate(series):
        s += f'<rect x="{70 + i * 150}" y="352" width="10" height="3" fill="{cc}"/>' + T(84 + i * 150, 356, name, 9.5, INK)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 372">{s}</svg>'


def g2():
    """变体净利（万元）与最大回撤双 panel 条形图。"""
    vs = ['BASE', 'A1', 'A1S', 'A2', 'B1', 'B2', 'B3', 'COMBO', 'COMBO2']
    nets = [R[v]['net_yuan'] / 1e4 for v in vs]
    mdds = [R[v]['mdd_yuan'] / 1e4 for v in vs]
    s = head('九个口径对比：净盈亏（上，万元）与最大回撤（下，万元）',
             '全 15 品种组合 · 2020-2026 · 红=亏损/恶化，蓝=盈利/改善 · COMBO2=A1S+A2+B2+B3（去掉实测有害的 B1）')
    x0, bw = 60, 62
    hi = max(max(nets), 0) + 10; lo = min(min(nets), 0) - 10
    y0, y1 = 175, 75
    def cy(v):
        return y1 + (hi - v) / (hi - lo) * (y0 - y1)
    s += L(x0, cy(0), 650, cy(0), '#B9B6AC')
    for i, v in enumerate(vs):
        x = x0 + i * bw + 8
        yy = cy(nets[i])
        cc = BLUE if nets[i] >= 0 else RED
        s += f'<rect x="{x}" y="{min(yy, cy(0)):.1f}" width="{bw - 22}" height="{abs(yy - cy(0)):.1f}" fill="{cc}" opacity="0.85" rx="2"/>'
        s += T(x + (bw - 22) / 2, yy - 4 if nets[i] >= 0 else yy + 11, f'{nets[i]:+.0f}', 8.5, cc, 'middle', '600')
        s += T(x + (bw - 22) / 2, y0 + 12, v, 8.5, INK, 'middle')
    # 回撤 panel
    y2, y3 = 205, 300
    mlo = min(mdds) - 10
    def cy2(v):
        return y2 + (0 - v) / (0 - mlo) * (y3 - y2)
    s += L(x0, y2, 650, y2, '#B9B6AC')
    for i, v in enumerate(vs):
        x = x0 + i * bw + 8
        hh = cy2(mdds[i]) - y2
        cc = GREEN if mdds[i] > -100 else ORANGE
        s += f'<rect x="{x}" y="{y2}" width="{bw - 22}" height="{hh:.1f}" fill="{cc}" opacity="0.8" rx="2"/>'
        s += T(x + (bw - 22) / 2, y2 + hh + 11, f'{mdds[i]:.0f}', 8.5, cc, 'middle', '600')
    s += T(x0, 330, '回撤越小越好；BASE 回撤 −131 万，COMBO2 收窄到 −78 万（−40%）', 9.5, MUT)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 344">{s}</svg>'


def g3():
    """高波日归因：BASE 高波日/平静日入场净盈亏 + 分年度对比。"""
    s = head('为什么「高波日不开仓」是最大单项改进：高波日入场归因 + 分年度净值（万元）',
             '左：BASE 按入场日是否高波（昨日振幅>滚动250日80分位）分组 · 右：剔除 IF/SC 的化工池分年度净盈亏')
    hv = BASE.groupby('hvol').net.sum() / 1e4
    hvn = BASE.groupby('hvol').size()
    x0, y0, y1 = 60, 240, 80
    lo, hi = -110, 70
    def cy(v):
        return y1 + (hi - v) / (hi - lo) * (y0 - y1)
    s += L(x0, cy(0), 300, cy(0), '#B9B6AC')
    for i, (lab, v, n, cc) in enumerate([('平静日入场', hv[False], hvn[False], BLUE),
                                         ('高波日入场', hv[True], hvn[True], RED)]):
        x = 90 + i * 110
        yy = cy(v)
        s += f'<rect x="{x}" y="{min(yy, cy(0)):.1f}" width="70" height="{abs(yy - cy(0)):.1f}" fill="{cc}" opacity="0.85" rx="3"/>'
        s += T(x + 35, yy - 5 if v >= 0 else yy + 13, f'{v:+.1f}万', 10, cc, 'middle', '700')
        s += T(x + 35, y0 + 14, lab, 10, INK, 'middle')
        s += T(x + 35, y0 + 28, f'{n} 笔', 9, MUT, 'middle')
    s += T(x0, y0 + 48, '高波日 903 笔亏 −100.7 万（avg −0.20R）；平静日 4302 笔赚 +61.1 万', 9.5, RED, weight='600')
    # 右：化工池分年度
    yrs = list(range(2020, 2027))
    bx = BASE[~BASE.sym.isin(['IF', 'SC'])].groupby('year').net.sum() / 1e4
    cx = C2[~C2.sym.isin(['IF', 'SC'])].groupby('year').net.sum() / 1e4
    qx0 = 350
    lo2, hi2 = -18, 6
    py0, py1 = 240, 80
    def cy2(v):
        return py1 + (hi2 - v) / (hi2 - lo2) * (py0 - py1)
    s += L(qx0, cy2(0), 660, cy2(0), '#B9B6AC')
    bw = 42
    for i, y in enumerate(yrs):
        x = qx0 + 8 + i * bw
        for j, (ser, cc) in enumerate([(bx, '#B9B6AC'), (cx, RED)]):
            v = ser.get(y, 0)
            yy = cy2(v)
            s += f'<rect x="{x + j * 19}" y="{min(yy, cy2(0)):.1f}" width="17" height="{abs(yy - cy2(0)):.1f}" fill="{cc}" rx="2"/>'
        s += T(x + 19, py0 + 12, str(y), 8.5, MUT, 'middle')
        s += T(x + 19, cy2(cx.get(y, 0)) - 3 if cx.get(y, 0) >= 0 else cy2(cx.get(y, 0)) + 10,
               f'{cx.get(y, 0):+.1f}', 7.5, RED, 'middle', '600')
    s += T(qx0, py0 + 32, '灰=BASE 红=COMBO2 · 化工池 6 年亏损收窄但仍未转正', 9.5, MUT)
    s += T(qx0, py0 + 46, '（策略毛利在化工上本来就薄，见正文归因）', 9.5, MUT)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 310">{s}</svg>'


# ---------- 表格 ----------
def fmt(v, d=1):
    return f'{v:+,.{d}f}' if v is not None else '—'


H = ['''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>融合策略程序化交易疏漏审查 + 研究结论融合回测</title>
<style>
body{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:960px;margin:0 auto;padding:24px;color:#22251F;background:#FDFCFB;line-height:1.75}
h1{font-size:22px;border-bottom:3px solid #2C2A24;padding-bottom:8px}
h2{font-size:17px;margin-top:34px;border-left:4px solid #C0392B;padding-left:10px}
h3{font-size:14px;margin-top:22px;color:#5A574E}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:12px 0}
th{background:#2C2A24;color:#fff;padding:6px 8px;text-align:right}
td{padding:5px 8px;border-bottom:1px solid #E8E4DA;text-align:right;font-variant-numeric:tabular-nums}
td.l,th.l{text-align:left}
.box{background:#F7F5F0;border:1px solid #E8E4DA;border-radius:8px;padding:12px 16px;margin:14px 0;font-size:13px}
.box.bad{border-left:4px solid #C0392B}
.box.good{border-left:4px solid #1E8449}
.box.info{border-left:4px solid #2166AC}
svg{width:100%;height:auto;margin:10px 0;border:1px solid #E8E4DA;border-radius:8px}
.mut{color:#8A877E;font-size:12px}
code{background:#F0EDE4;padding:1px 5px;border-radius:4px;font-size:12px}
.r{color:#C0392B;font-weight:700}.g{color:#1E8449;font-weight:700}
ol.tight,ul.tight{margin:6px 0;padding-left:22px}ol.tight li,ul.tight li{margin:3px 0}
</style></head><body>
<h1>融合策略 V3.4 · 程序化交易疏漏审查 + 六位编码研究结论融合回测</h1>
<p class="mut">数据：15 品种 1 分钟 → 小时线（夜盘归次日交易日）· 2020-01 ~ 2026-06 · 引擎逐行移植 <code>app/strategies/fusion_signal.py::walk_fusion_states</code>（V3.4 定稿参数：EMA140 方向 / MA20 回踩+10根突破 / ADX≥15 / 斐波汇流 / 2×ATR 初始+吊灯 / 0.5ATR 保本 / 利弗莫尔加码上限 2 手 / 冷却 3 根）· 成本 1.5bp+1跳/边 · 脚本 <code>step43_fusion_bt.py</code> / <code>step44_fusion_report.py</code></p>
''']

# ================= 一、疏漏审查 =================
H.append('''<h2>一、程序化交易疏漏审查（按严重度排序）</h2>
<p>先说结论：<b>策略的规则工程化程度已经很高</b>（无未来函数、口径单一真源、成本 TCA、熔断、持仓独占都有），真正的疏漏集中在<b>「品种适配性」和「事前风控」</b>两层。以下每条都附回测证据。</p>

<h3>🔴 疏漏 1：品种适配性没有闭环——策略在化工池上毛期望就是负的</h3>
<div class="box bad">V2.0 手册注明「农产品 −2.2% 不适用」、黑色系 8 池净利 +100 万；但<b>从未对化工池下过判决</b>。本次在 13 个化工/能源品种上复刻 V3.4 实跑：<b>剔除 IF 后 14 品种毛利 −13.4 万（没扣成本就亏钱）</b>，扣成本后 −66.0 万。分品种只有 EG/FG/SA/EB 微利。策略的毛利几乎全部来自 IF（+47.8 万毛利）。<br>
<b>依据</b>：与六位编码研究互证——化工 1 分钟尺度方向不可预测（slot 条件增益≈0）、突破支路在化工是负期望（见疏漏 4）。<b>黑色系（RB/HC/I/JM）的趋势性强是因为产业资金+库存周期驱动，化工品受原油/进口成本扰动，趋势连续性天然差。</b><br>
<b>建议</b>：化工池只做 FG/SA/EG/EB 四个历史毛利为正的，或干脆不做；把 MA/PP/L/PF/PX/PR/UR/SC 移出池。</div>

<h3>🔴 疏漏 2：只有「事后熔断」，没有「事前降档」——高波日开仓是最大出血点</h3>
<div class="box bad">把 BASE 全部 5,205 笔按「入场前一天是否高波日」（昨日振幅 > 滚动 250 日 80 分位）分组：<br>
<b>高波日入场 903 笔：净亏 −100.7 万，笔均 −0.20R，胜率 19.4%；平静日 4,302 笔：净赚 +61.1 万。</b><br>
现有风控只有月度/单日回撤熔断（亏完才停）和连亏暂停（事后）。高波日的典型形态是跳空+宽幅震荡，2×ATR 止损在开盘根被噪声扫掉的概率骤增——这是<b>结构性亏损时段</b>，不是运气。<br>
<b>社会学依据</b>：极端波动日市场参与结构改变——散户 FOMO 涌入+止损链触发，价格发现让位于流动性挤出；趋势跟随系统的「突破」恰在这天最容易是假突破。<br>
<b>建议</b>：加事前闸——昨日振幅超 80 分位 → 当日不开新仓（A1S，实测单项 −39.6 万 → +25.9 万）。</div>

<h3>🟠 疏漏 3：组合层没有板块簇集中度控制</h3>
<div class="box">手册只卡「同时持仓 ≤6 个品种」，但 6 个品种若全在同一产业链（如 TA/PX/PF/PR 聚酯链），按我们实测的共尾倍数（簇内同向 4.7×），<b>等于 1 个品种开 4 倍仓</b>。2026-04-08 挤仓崩塌日 MA/EG 同日 −15% 级下跌就是实例。V3.4 加码机制（上限 2 手）会进一步放大簇内暴露。<br>
<b>建议</b>：簇内同时持仓 ≤2（A2 实测小幅改善回撤）；簇内任一品种单日 |r|>3σ 时，其余当日不开仓。</div>

<h3>🟠 疏漏 4：突破支路（both_nm 的 bk）在化工池是负期望</h3>
<div class="box">BASE 按入场类型分解：<b>突破单 3,285 笔亏 −73.3 万；回踩单 1,920 笔赚 +33.7 万</b>。突破在黑色系有效（焦炭铁矿趋势启动快），在化工上被开盘噪声和假突破收割。与 1 分钟研究「开盘追突破=陷阱」「极端收盘会回拉」同向互证。<br>
<b>建议</b>：化工品种 entry_mode 改 <code>pullback</code>（只回踩）；黑色/IF 保留 both_nm。这是一条配置项就能解决的疏漏。</div>

<h3>🟠 疏漏 5：成本口径对化工池不成立</h3>
<div class="box">手册口径「盈亏平衡往返 55bp、成本须 ≤20bp」来自黑色池。化工池实测：<b>毛利 34.4 万 vs 成本 74.0 万——成本是毛利的 2.2 倍</b>，盈亏平衡无从谈起。TA/PX/PF/PR tick=2 元、乘数 5，单边滑点占比天然高于黑色系。<br>
<b>建议</b>：品种准入门槛改为「该品种回测毛利/成本 ≥ 1.5」，而不是全池统一 20bp。</div>

<h3>🟡 疏漏 6：开盘根 / 尾盘根无特殊处理（实测影响中等）</h3>
<div class="box">开盘首根（夜盘 21:00、日盘 09:00）的信号由隔夜跳空主导：B2 变体（开盘根不开仓）−46.4 万 vs BASE −39.6 万，<b>略有害</b>（开盘根的趋势信号在 IF 上是真趋势）；B3（尾盘根不开仓）+14.4 万，<b>有益</b>（14:00/22:00 根的突破多为尾盘平仓噪声）。结论：只采纳 B3，不采纳 B2——与 1 分钟研究「开盘 15 分钟不做反转单」不矛盾（本策略是顺势，不是反转）。</div>

<h3>🟡 疏漏 7（澄清，非错误）：1 分钟的「极端收盘等回调」不能移植到小时尺度</h3>
<div class="box info">B1 变体（信号根极端收盘 → 次根 0.25ATR 限价等回调）实测 −77.5 万，<b>比基线多亏 38 万</b>。1 分钟尺度捡的 ~1bp 是买卖价差的影子（微观结构回归），小时尺度延迟入场丢掉的是趋势启动段——<b>同一现象在两个尺度上方向相反</b>。这条写进手册可以防止后人误用。</div>

<h3>✅ 做得对、不要动的部分</h3>
<ul class="tight">
<li>无前视纪律：方向层用前一根 EMA140、ADX 用上一根、突破用 REF(HHV,1)、斐波用已确认枢轴——逐行核对无误；</li>
<li>离场三件套（2×ATR 初始 + 吊灯 + 0.5ATR 粘性保本）在 6.5 年化工池上把单笔亏损封在 −1.05R 左右；</li>
<li>冷却 3 根 + 持仓独占 + 不加码网格化纪律；</li>
<li>成本 TCA 台账与「回测 vs 实盘偏差」清单（手册 §7）——这是绝大多数程序化新手缺的东西。</li>
</ul>
''')

# ================= 二、融合回测 =================
vs = ['BASE', 'A1', 'A1S', 'A2', 'B1', 'B2', 'B3', 'COMBO', 'COMBO2']
names = {'BASE': '基线 V3.4 原样', 'A1': 'A1 高波日减半+宽止损', 'A1S': 'A1S 高波日不开仓',
         'A2': 'A2 簇内同时持仓≤2', 'B1': 'B1 极端收盘等回调', 'B2': 'B2 开盘根不开仓',
         'B3': 'B3 尾盘根不开仓', 'COMBO': 'COMBO 全融合(含B1)', 'COMBO2': 'COMBO2 融合(去B1)'}
H.append('<h2>二、研究结论融合回测：九口径对比</h2>')
H.append(g1())
H.append(g2())
H.append('''<table>
<tr><th class="l">口径</th><th>笔数</th><th>胜率</th><th>盈亏比PF</th><th>净盈亏(万)</th><th>毛利(万)</th><th>最大回撤(万)</th><th>MAR</th><th>holdout2026(万)</th><th>holdout PF</th></tr>''')
for v in vs:
    m = R[v]
    ho = m.get('holdout', {})
    net = m['net_yuan'] / 1e4
    cl = 'g' if net > 0 else 'r'
    row_hl = ' style="background:#F4FAF6"' if v in ('A1S', 'COMBO2') else ''
    H.append(f'<tr{row_hl}><td class="l"><b>{v}</b> {names[v]}</td><td>{m["n"]:,}</td>'
             f'<td>{m["win"]*100:.1f}%</td><td>{m["pf"]:.3f}</td>'
             f'<td class="{cl}">{net:+.1f}</td><td>{m.get("gross_yuan",0)/1e4:+.1f}</td>'
             f'<td>{m["mdd_yuan"]/1e4:.1f}</td><td>{m["mar"]:.2f}</td>'
             f'<td class="{"g" if ho.get("net_yuan",0)>0 else "r"}">{ho.get("net_yuan",0)/1e4:+.1f}</td>'
             f'<td>{ho.get("pf",0):.3f}</td></tr>')
H.append('</table>')
H.append(g3())

# ================= 三、结论 =================
H.append('''<h2>三、结论与落地建议</h2>
<div class="box good"><b>最有效的三个改动（按边际贡献排序，均有 holdout 验证）：</b>
<ol class="tight">
<li><b>A1S 高波日不开仓</b>（−39.6万 → +25.9万，单项 +65.5 万）：昨日振幅 > 滚动 250 日 80 分位 → 当日全品种不开新仓。holdout 2026 极端年从 −51.2 万收窄到 −3.3 万。这是「波动可预测」研究结论的直接变现——<b>不能预测方向，但可以预测「今天不该开仓」</b>。</li>
<li><b>B3 尾盘根不开仓</b>（单项 +14.4 万）：14:00 / 22:00（及 SC 02:00）根不追突破。</li>
<li><b>A2 簇内持仓 ≤2</b>（回撤 −131 → −127 万，小幅）：防 2026-04 式板块共振。</li>
</ol>
<b>组合 COMBO2</b>（A1S+A2+B2+B3）：净 +59.1 万 / 回撤 −78.3 万 / MAR 0.75 / holdout 转正 +9.3 万（PF 1.23）——对比基线净 −39.6 万 / 回撤 −131 万 / holdout −51.2 万。<b>注意 B2 在组合里仍轻微拖累，可选；B1 明确剔除。</b></div>

<div class="box bad"><b>但必须诚实说清的三条边界：</b>
<ol class="tight">
<li><b>剔除 IF 后，化工池 COMBO2 仍亏 −9.3 万</b>（基线 −29.2 万）。融合点解决的是「少亏、控回撤、极端年存活」，<b>不能把化工池变成正期望</b>。化工池的根本问题是毛利太薄（成本是毛利 2.2 倍），这是品种适配问题，不是参数问题。</li>
<li><b>组合净利高度依赖 IF</b>（COMBO2 的 IF 贡献 +68.5 万中的大部分）。IF 一只手数乘数 300，波动天然大；组合结论对 IF 在池与否敏感。</li>
<li>A2 是<b>事后过滤近似</b>（被拦的交易不会改变该品种后续信号路径），真实联动的差异会略大；上线前需在引擎内实现交互式簇控重跑。</li>
</ol></div>

<div class="box info"><b>给你（4 万本金 · 固定 1 手）的最终配置建议：</b><br>
① 品种池：<b>IF 不在你的资金量级内</b>（单手保证金 ~14 万），本回测对你直接可用的结论是——化工池即使加了全部融合点也只是「少亏」，<b>不建议把融合策略跑在纯化工池上</b>；继续做手册的黑色 5 池（RB/HC/FG/BU/SP），其中 FG 我们验证了（BASE +0.79 万 → COMBO2 +3.77 万）。<br>
② 必加的一条规则：<b>昨日振幅 > 80 分位 → 今日不开新仓</b>。每天开盘前看一眼就能执行，单项价值 +65 万（本池实测）。<br>
③ 14:00 和 22:00 那根小时线不出信号则已，出了也<b>不做</b>。<br>
④ 簇内（你池里的黑色链 RB/HC 算一簇）同时持仓 ≤2。<br>
⑤ 不要给策略加「极端收盘等回调再进」——小时尺度上实测有害。</div>

<h2>四、口径与局限声明</h2>
<ul class="tight mut">
<li>引擎为逐行移植 V3.4 主循环（方向 EMA140 前一根 / ADX≥15 上一根 / 斐波汇流 W=60 / 加码上限 2 手），未与生产库 hourly_bar 逐位对拍（本地 Docker/DB 当前未运行）；小时线桶为「td × 时钟小时」，与交易所官方 60 分棒在 10:15 休市处的切分可能差 1-2 根/日。</li>
<li>成交价 = 信号根/离场根收盘价（close-only，与生产口径一致）；成本 = 1.5bp + 1跳/边；未建模涨跌停无法成交（手册 R4 已列为实盘风险）。</li>
<li>合约乘数/最小价位：PR（瓶片）按 15 吨/手、2 元跳为假设值；其余为交易所标准值。</li>
<li>高波日判定用「昨日日振幅 > 过去 250 个交易日 80 分位」，昨日收盘即可算，无前视。</li>
<li>holdout = 2026 年（极端挤仓年，1-6 月），train = 2020-2025；所有改进在 holdout 同号，无过拟合迹象。</li>
</ul>
<p class="mut">生成：step43_fusion_bt.py（回测）· step44_fusion_report.py（报告）· 数据 results_fusion_bt.json · 交易明细 trades_BASE.csv / trades_COMBO2.csv</p>
</body></html>''')

open('fusion_review_report.html', 'w', encoding='utf-8').write(''.join(H))
print('saved fusion_review_report.html')
