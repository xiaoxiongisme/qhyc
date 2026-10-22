# -*- coding: utf-8 -*-
"""step45 融合策略 V4-RC 升级版报告（fusion_v4_report.html）"""
import json
import numpy as np
import pandas as pd

R = json.load(open('results_fusion_bt.json'))
BASE = pd.read_csv('trades_BASE.csv', parse_dates=['edt', 'xdt'])
V4 = pd.read_csv('trades_V4NP2.csv', parse_dates=['edt', 'xdt'])
V5 = pd.read_csv('trades_V4NP5.csv', parse_dates=['edt', 'xdt'])

INK = '#22251F'; MUT = '#8A877E'; GRID = '#E8E4DA'; PANEL = '#F7F5F0'
RED = '#C0392B'; GREEN = '#1E8449'; BLUE = '#2166AC'; ORANGE = '#D68910'; GRAY = '#B9B6AC'


def head(title, sub):
    return (f'<rect width="680" height="60" fill="{PANEL}" stroke="{GRID}"/>'
            f'<text x="16" y="24" font-size="14" font-weight="700" fill="{INK}">{title}</text>'
            f'<text x="16" y="44" font-size="10.5" fill="{MUT}">{sub}</text>')


def T(x, y, s, sz=10, fill=INK, anchor='start', weight='400'):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{sz}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


def L(x1, y1, x2, y2, c=GRID, w=1):
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="{w}"/>'


def g1():
    """净值曲线：BASE / V4NP2 / V4NP5。"""
    series = [('基线 V3.4', BASE, GRAY), ('V4-RC（全品种）', V4, BLUE), ('V4-RC+品种门', V5, RED)]
    t0 = np.datetime64('2020-01-01'); t1 = np.datetime64('2026-07-01')
    x0, x1 = 60, 660
    def tx(t):
        return x0 + (t - t0) / (t1 - t0) * (x1 - x0)

    def eq(df):
        d = df.sort_values('xdt')
        return d.xdt.values, d.net.cumsum().values / 1e4
    ys = np.concatenate([eq(df)[1] for _, df, _ in series])
    lo, hi = ys.min(), ys.max()
    y0, y1 = 330, 80
    def cy(v):
        return y1 + (hi - v) / (hi - lo) * (y0 - y1)
    s = head('净值曲线：基线 V3.4 vs 升级版 V4-RC（万元，按平仓时间累计）',
             '15 品种 · 2020-01~2026-06 · 成本 1.5bp+1跳/边 · V4-RC = P2高波日不开仓 + P3簇上限+簇冲击日 + P4化工只回踩 + P6尾盘根不开仓')
    for yr in range(2020, 2027):
        x = tx(np.datetime64(f'{yr}-01-01'))
        s += L(x, y1, x, y0) + T(x, y0 + 14, str(yr), 9, MUT, 'middle')
    for gv in np.arange(np.floor(lo / 20) * 20, hi + 20, 20):
        s += L(x0, cy(gv), x1, cy(gv)) + T(x0 - 6, cy(gv) + 3, f'{gv:.0f}', 9, MUT, 'end')
    s += L(x0, cy(0), x1, cy(0), '#B9B6AC')
    for name, df, cc in series:
        xs, yv = eq(df)
        pts = ' '.join(f'{tx(t):.1f},{cy(v):.1f}' for t, v in zip(xs, yv))
        s += f'<polyline points="{pts}" fill="none" stroke="{cc}" stroke-width="1.8"/>'
    for i, (name, _, cc) in enumerate(series):
        s += f'<rect x="{70 + i * 190}" y="352" width="10" height="3" fill="{cc}"/>' + T(84 + i * 190, 356, name, 9.5, INK)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 372">{s}</svg>'


def g2():
    """分年度净盈亏：BASE vs V4NP2（全池）。"""
    yrs = list(range(2020, 2027))
    bx = BASE.groupby('year').net.sum() / 1e4
    cx = V4.groupby('year').net.sum() / 1e4
    lo, hi = min(bx.min(), cx.min()) - 8, max(bx.max(), cx.max()) + 8
    x0, y0, y1 = 60, 250, 70
    def cy(v):
        return y1 + (hi - v) / (hi - lo) * (y0 - y1)
    s = head('分年度净盈亏（万元）：基线 vs V4-RC 全品种',
             '灰=基线 · 蓝=V4-RC · 2021/2025 两年基线大亏，V4-RC 分别收窄到 −2.9万 / −10.2万；2026 极端年反转为 +16.6万')
    for gv in range(-50, 60, 10):
        if lo <= gv <= hi:
            s += L(x0, cy(gv), 660, cy(gv)) + T(x0 - 6, cy(gv) + 3, f'{gv}', 9, MUT, 'end')
    s += L(x0, cy(0), 660, cy(0), '#B9B6AC')
    bw = 92
    for i, y in enumerate(yrs):
        x = x0 + 20 + i * bw
        for j, (ser, cc) in enumerate([(bx, GRAY), (cx, BLUE)]):
            v = ser.get(y, 0)
            yy = cy(v)
            s += f'<rect x="{x + j * 42}" y="{min(yy, cy(0)):.1f}" width="38" height="{abs(yy - cy(0)):.1f}" fill="{cc}" rx="2"/>'
            s += T(x + 19 + j * 42, yy - 4 if v >= 0 else yy + 11, f'{v:+.1f}', 8, cc, 'middle', '600')
        s += T(x + 42, y0 + 14, str(y), 9, MUT, 'middle')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 290">{s}</svg>'


def g3():
    """单项贡献条形（对 BASE 的净利增量）。"""
    items = [('P4 化工只回踩（IF保留突破）', R['P4']['net_yuan'] - R['BASE']['net_yuan']),
             ('P2 高波日不开仓', R['P2']['net_yuan'] - R['BASE']['net_yuan']),
             ('P6 尾盘根不开仓', R['P6']['net_yuan'] - R['BASE']['net_yuan']),
             ('P3 簇上限+簇冲击日', R['P3']['net_yuan'] - R['BASE']['net_yuan']),
             ('P7 极端收盘ADX确认（否决）', R['P7']['net_yuan'] - R['BASE']['net_yuan']),
             ('B2 开盘根不开仓（否决）', R['B2']['net_yuan'] - R['BASE']['net_yuan']),
             ('B1 极端收盘等回调（否决）', R['B1']['net_yuan'] - R['BASE']['net_yuan'])]
    x0, y0, y1 = 60, 250, 74
    lo, hi = -45, 55
    def cy(v):
        return y1 + (hi - v) / (hi - lo) * (y0 - y1)
    s = head('单项贡献：相对基线净利增量（万元）',
             'P4/P2/P6 为正贡献；P3 中性；P7/B2/B1 经实测否决（红=有害）——升级版只收录被数据支持的四项')
    s += L(x0, cy(0), 660, cy(0), '#B9B6AC')
    for i, (name, v) in enumerate(items):
        y = y1 + 12 + i * 24
        cc = GREEN if v > 0 else RED
        s += T(x0, y + 4, name, 9.5, INK)
        x_end = 330 + v / 55 * 300
        s += f'<rect x="{min(330, x_end):.1f}" y="{y - 8}" width="{abs(x_end - 330):.1f}" height="14" fill="{cc}" opacity="0.85" rx="2"/>'
        s += T(max(330, x_end) + 6 if v >= 0 else min(330, x_end) - 6, y + 4,
               f'{v:+.1f}', 9, cc, 'start' if v >= 0 else 'end', '700')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 268">{s}</svg>'


H = ['''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>融合策略 V4-RC 升级版 · 回测报告</title>
<style>
body{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:960px;margin:0 auto;padding:24px;color:#22251F;background:#FDFCFB;line-height:1.75}
h1{font-size:22px;border-bottom:3px solid #2C2A24;padding-bottom:8px}
h2{font-size:17px;margin-top:34px;border-left:4px solid #2166AC;padding-left:10px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:12px 0}
th{background:#2C2A24;color:#fff;padding:6px 8px;text-align:right}
td{padding:5px 8px;border-bottom:1px solid #E8E4DA;text-align:right;font-variant-numeric:tabular-nums}
td.l,th.l{text-align:left}
.box{background:#F7F5F0;border:1px solid #E8E4DA;border-radius:8px;padding:12px 16px;margin:14px 0;font-size:13px}
.box.bad{border-left:4px solid #C0392B}.box.good{border-left:4px solid #1E8449}.box.info{border-left:4px solid #2166AC}
svg{width:100%;height:auto;margin:10px 0;border:1px solid #E8E4DA;border-radius:8px}
.mut{color:#8A877E;font-size:12px}code{background:#F0EDE4;padding:1px 5px;border-radius:4px;font-size:12px}
.r{color:#C0392B;font-weight:700}.g{color:#1E8449;font-weight:700}
</style></head><body>
<h1>融合策略 V4-RC 升级版 · 六项优化融合回测报告</h1>
<p class="mut">基线 = V3.4 生产口径逐行移植（EMA140 方向 / MA20 回踩 + 10 根突破 / ADX≥15 / 斐波汇流 / 2ATR 初始+吊灯 / 0.5ATR 保本 / 加码上限 2 / 冷却 3 根）。升级版在基线之上叠加审查报告疏漏 2/3/4/5/6/7 的优化。脚本 <code>step43_fusion_bt.py</code>（24 个口径）· <code>step45_fusion_v4_report.py</code>。生产环境未做任何改动。</p>
''']

H.append('''<h2>一、升级版规则定义（V4-RC = 基线 + 四项收录 + 一项可选门 + 两项否决）</h2>
<table>
<tr><th class="l">编号</th><th class="l">规则</th><th class="l">来源疏漏</th><th class="l">实现</th></tr>
<tr><td><b>P2</b></td><td class="l"><b>高波日不开新仓</b>：昨日日振幅 > 过去 250 个交易日 80 分位 → 当日全品种不接受新入场信号（持仓照常按原止损/吊灯管理）</td><td class="l">#2 无事前降档</td><td class="l">每日开盘前可算，无前视；阈值 50-90 分位均正贡献（见 §3）</td></tr>
<tr><td><b>P3</b></td><td class="l"><b>簇内同时持仓 ≤2</b> + <b>簇冲击日封禁</b>：簇日收益 |r|>3σ（滚动 250 日）当日，该簇全部品种不开新仓</td><td class="l">#3 无板块簇控制</td><td class="l">聚酯/聚烯烃/两碱/醇醚四簇；独立仓品种不受限</td></tr>
<tr><td><b>P4</b></td><td class="l"><b>化工品种只走回踩支路</b>（entry_mode=pullback），IF 保留 both_nm（突破）</td><td class="l">#4 突破负期望</td><td class="l">配置项切换；IF 单独设回踩会从 +26.4 万翻成 −6.9 万，必须保留突破</td></tr>
<tr><td><b>P6</b></td><td class="l"><b>尾盘根不开仓</b>：每个交易日夜盘末根、日盘末根（14:00/22:00，SC 为 02:00 根）不接受新入场</td><td class="l">#6 时段无区分</td><td class="l">开盘根不做同样处理（B2 实测有害 −6.8 万，否决）</td></tr>
<tr><td><b>P5（可选）</b></td><td class="l"><b>品种门</b>：只保留 P4 之后重估 gross/cost ≥1.5 的品种（EB/SC/FG/SA/IF/V）</td><td class="l">#5 成本口径</td><td class="l">⚠ 用 P4 之前证据设门（只留 EB/IF）holdout 反噬 −4.9 万——门必须在行为变更后重估</td></tr>
<tr><td><b>P7（否决）</b></td><td class="l">极端收盘信号要求 ADX≥20 才开仓</td><td class="l">#7 尺度边界</td><td class="l">实测 −20.1 万（vs 基线 −39.6→−59.6），否决；结论是保持即时入场、不加过滤</td></tr>
<tr><td><b>B1（否决）</b></td><td class="l">极端收盘等回调再入场</td><td class="l">#7 尺度边界</td><td class="l">−38.0 万，否决（1 分钟结论不可外推到小时尺度）</td></tr>
</table>
<div class="box info"><b>一句话版本：</b>「高波日歇、尾盘歇、化工只低吸、簇内不重仓、IF 照常追突破、信号来了马上进不等回调」。</div>
''')

H.append('<h2>二、主结果：24 口径对比（全池 15 品种）</h2>')
H.append(g1())
rows = [
    ('BASE', '基线 V3.4'), ('P2', 'P2 高波日不开仓'), ('P3', 'P3 簇控+冲击日'), ('P4', 'P4 化工只回踩'),
    ('P6', 'P6 尾盘根不开仓'), ('P7', 'P7 极端收盘ADX（否决）'), ('B1', 'B1 等回调（否决）'),
    ('B2', 'B2 开盘根禁入（否决）'), ('COMBO2', '旧融合版 COMBO2'),
    ('V4NP2', '★ V4-RC 全品种'), ('V4NP5', '★ V4-RC+品种门'), ('V4NP3', 'V4-RC+阈值70'),
]
H.append('''<table>
<tr><th class="l">口径</th><th>笔数</th><th>胜率</th><th>PF</th><th>净盈亏(万)</th><th>最大回撤(万)</th><th>MAR</th><th>train(万)</th><th>holdout 2026(万)</th><th>holdout PF</th></tr>''')
for v, nm in rows:
    m = R[v]; tr, ho = m.get('train', {}), m.get('holdout', {})
    net = m['net_yuan'] / 1e4
    cl = 'g' if net > 0 else 'r'
    hl = ' style="background:#F0F7FF"' if v in ('V4NP2', 'V4NP5') else ''
    H.append(f'<tr{hl}><td class="l"><b>{nm}</b></td><td>{m["n"]:,}</td><td>{m["win"]*100:.1f}%</td>'
             f'<td>{m["pf"]:.3f}</td><td class="{cl}">{net:+.1f}</td><td>{m["mdd_yuan"]/1e4:.1f}</td>'
             f'<td>{m["mar"]:.2f}</td><td>{tr.get("net_yuan",0)/1e4:+.1f}</td>'
             f'<td class="{"g" if ho.get("net_yuan",0)>0 else "r"}">{ho.get("net_yuan",0)/1e4:+.1f}</td>'
             f'<td>{ho.get("pf",0):.2f}</td></tr>')
H.append('</table>')
H.append(g3())

H.append('''<div class="box good"><b>核心结果：</b>
<ul class="tight">
<li><b>V4-RC 全品种（V4NP2）：+108.7 万，PF 1.25，回撤 −44.5 万，MAR 2.44</b>——对比基线 −39.6 万 / PF 0.96 / 回撤 −131.2 万 / MAR −0.30。净利改善 <b>+148.3 万</b>，回撤收窄 66%。</li>
<li><b>样本外（2026 极端年）反转为正：+16.6 万，PF 1.48</b>（基线 holdout −51.2 万 / PF 0.60）。train 期 +92.1 万 / PF 1.23——train 与 holdout 同向，无过拟合迹象。</li>
<li>加品种门（V4NP5）：+116.5 万 / PF 1.29 / 回撤 −40.0 万 / <b>MAR 2.92</b> / holdout +16.1 万（PF 1.50）。代价是笔数减半（877 笔），容量下降。</li>
<li>单项贡献：<b>P4 +48.5 万 &gt; P2 +65.4 万 &gt; P6 +14.4 万</b>，P3 +2.5 万（中性偏正）。SC 一个品种在 P4 之后从 −36.8 万翻成 +59.1 万，是最大单点翻转。</li>
</ul></div>''')

H.append('<h2>三、稳健性：高波日阈值扫描</h2>')
H.append('''<table>
<tr><th>高波日阈值</th><th>50 分位</th><th>60</th><th>70</th><th>75</th><th><b>80（采用）</b></th><th>85</th><th>90</th><th>95</th></tr>
<tr><td class="l"><b>净盈亏(万)</b></td>''' + ''.join(
    f'<td class="{"g" if R["A1S" if q == 80 else f"THR{q}"]["net_yuan"] > 0 else "r"}">'
    f'{R["A1S" if q == 80 else f"THR{q}"]["net_yuan"]/1e4:+.1f}</td>'
    for q in [50, 60, 70, 75, 80, 85, 90, 95]) + '</tr></table>')
H.append('<p class="mut">除 95 分位（几乎不触发，≈基线）外，50-90 分位全部正贡献——「高波日歇」不是精细调参的结果，是一个平坦的稳健结论。采用 80 分位（与研究报告一致）；70 分位样本内更优（+71.1 万 vs +25.9 万）但牺牲 20% 交易量，且阈值敏感性总体平缓，不值得为样本内数字移动。</p>')
H.append(g2())

H.append('''<h2>四、方法论教训（本轮新增，比结论更值得带走）</h2>
<div class="box bad"><b>教训 1：品种门必须「行为变更后重估」。</b>用 P4 之前的证据设品种门（只留 EB/IF），V4 的 holdout 反噬 −4.9 万（n=19）；而按 P4 之后重估（留 EB/SC/FG/SA/IF/V）的 V4NP5 holdout +16.1 万。<b>同一个门，证据的新旧决定了它是毒药还是解药</b>——因为 P4 把 SC 从最差品种变成了最好品种（−36.8 万 → +59.1 万）。</div>
<div class="box bad"><b>教训 2：IF 的突破支路是真 alpha，不能一刀切。</b>全品种改 pullback-only 后 IF 从 +26.4 万翻成 −6.9 万。股指的 10 根突破是有效信号，化工的不是——同一条规则在不同品种上方向相反，必须逐品种验证。</div>
<div class="box"><b>教训 3：被数据否决的优化也是产出。</b>P7（ADX 确认）和 B1（等回调）单测都是负贡献，写进「不做清单」比写进策略更有价值——它们防止未来某轮「优化」把系统改坏。</div>

<h2>五、口径与局限</h2>
<ul class="tight mut">
<li>引擎逐行移植 V3.4 主循环，未与生产库 hourly_bar 逐位对拍（本地 DB 未运行）；小时线按「td × 时钟小时」分桶，与官方 60 分棒在 10:15 休市处可能差 1-2 根/日。</li>
<li>成交价=收盘价（close-only，与生产一致）；成本=1.5bp+1 跳/边；未建模涨跌停。</li>
<li>P4/P5 的逐品种配置由 train 期（2020-2025）证据决定，holdout（2026）仅作验证——这是合理的 walk-forward 用法，但 P5 门使 V4NP5 的有效样本内品种数只有 6 个，容量与分散度低于 V4NP2。</li>
<li>A3 簇冲击日用簇等权日收益的 3σ，σ 为滚动 250 日估计；A2 簇上限为事后贪心近似（被拦交易不改变该品种后续路径）。</li>
<li>PR 乘数按 15 吨/手假设；SC 夜盘至 02:29，其「尾盘根」为 02:00 根。</li>
</ul>
<p class="mut">生成：step43_fusion_bt.py（回测，24 口径）· step45_fusion_v4_report.py（报告）· 交易明细 trades_V4NP2.csv / trades_V4NP5.csv。生产环境（app/、scripts/）未做任何改动，升级规则以上述参数表形式交付，可直接落到 config。</p>
</body></html>''')

open('fusion_v4_report.html', 'w', encoding='utf-8').write(''.join(H))
print('saved fusion_v4_report.html')
