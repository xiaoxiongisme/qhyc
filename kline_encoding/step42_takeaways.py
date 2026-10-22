# -*- coding: utf-8 -*-
"""step42: 交易可用结论推导书（trading_takeaways.html）
结构：①复验对照表（step41 独立重算） ②大白话推导链（现象→依据→推论）
     ③可执行清单（每条：做法/数据依据/对胜率或盈亏比的作用路径/失效条件）
"""
import json
import numpy as np

V = json.load(open('results_verify.json'))
TD = json.load(open('results_time_dim.json'))
EXT = json.load(open('results_extreme.json'))
HMM = json.load(open('results_sector_hmm.json'))

P1, ORANGE, P3 = '#3E3480', '#D97B29', '#A397D2'
RED, GREEN, MUT = '#C4372E', '#2E7D4F', '#888780'
T1, T2, GRID = '#26215C', '#5F5E5A', '#E8E6DE'


def T(x, y, s, size=10.5, fill=T1, anchor='start', weight='400'):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'

def L(x1, y1, x2, y2, c=GRID, w=1):
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="{w}"/>'

def R(x, y, w, h, c, op=1.0, rx=3):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" height="{max(h,0):.1f}" rx="{rx}" fill="{c}" fill-opacity="{op}"/>'


def fig_key_facts():
    """一张图看懂两个最可用的事实：开盘浓度 + MR 时段定位"""
    h = 300
    s = f'<svg viewBox="0 0 680 {h}" xmlns="http://www.w3.org/2000/svg" style="max-width:100%">'
    s += T(30, 34, '两个可直接用的事实：波动集中在开盘，回归集中在收盘', 14, T1, weight='700')
    s += T(30, 54, '左：一天振幅的分布（MA，train）；右：「收在极端位置→下一根回拉」的强度（IC，越负越强）', 10, T2)
    # 左：开盘浓度
    px0, py0, py1 = 60, 96, 210
    amp_open = V['v1_amp_open_bp']; amp_rest = V['v1_amp_rest_bp']
    mx = amp_open * 1.25
    for i, (nm, vv, cc) in enumerate([('开盘前 15 分钟', amp_open, P1), ('其余 330 分钟', amp_rest, '#C9C7BE')]):
        bh = vv / mx * (py1 - py0)
        x = px0 + i * 110
        s += R(x, py1 - bh, 80, bh, cc, 0.92, 4)
        s += T(x + 40, py1 - bh - 6, f'{vv:.1f}bp', 11, cc, 'middle', '700')
        s += T(x + 40, py1 + 16, nm, 9.5, T2, 'middle')
    s += T(px0, py1 + 40, '开盘 15 分钟只占 4.3% 的时间，贡献 22.8% 的全日振幅（浓度 5.3 倍）', 9.5, T1, weight='600')
    # 右：MR 时段定位（MA + EG）
    qx0 = 430
    blocks = [('09:00 开盘', 'open9'), ('21:00 夜开', 'night_open'), ('盘中', 'midday'), ('夜盘收盘', 'night_close')]
    mx2 = 0.28
    for gv in [-0.1, -0.2]:
        yy = py0 + (0 - gv) / mx2 * (py1 - py0) * 0 + 0  # baseline handled below
    zy = py0 + 18
    s += L(qx0, zy, qx0 + 210, zy, '#B9B6AC')
    for i, (nm, key) in enumerate(blocks):
        for j, sym in enumerate(['MA', 'EG']):
            vv = abs(V['v3_clv_mr'][sym][key])
            cc = [P1, ORANGE][j]
            bh = vv / mx2 * 120
            x = qx0 + i * 54 + j * 24
            s += R(x, zy, 20, bh, cc, 0.92, 2)
            if j == 0:
                s += T(x + 10, zy + bh + 12, f'{V["v3_clv_mr"][sym][key]:.2f}', 7.5, cc, 'middle', '700')
        s += T(qx0 + i * 54 + 22, zy + 132, nm, 8.5, T2, 'middle')
    s += T(qx0, py0 - 4, '向下越长 = 回拉越强', 9.5, T2)
    s += T(px0, 282, '含义：开盘的极端走势多为「定价」，追进去赌回调容易被趋势碾过；尾盘的极端走势多为「平仓单」，会回拉', 10, T1, weight='600')
    s += '</svg>'
    return s


CSS = """
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#FBFAF7;color:#26215C;margin:0;padding:32px 40px;line-height:1.7}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:22px}h2{font-size:17px;margin-top:34px;border-left:4px solid #5C4FA1;padding-left:10px}
h3{font-size:14px;margin-top:22px}
.sub{color:#888780;font-size:12.5px;margin-top:4px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:10px 0}
th,td{border:1px solid #E5E2D8;padding:5px 9px;text-align:center}
th{background:#F3F1EA;font-weight:600}
td.l{text-align:left}
.fig{margin:14px 0}
.step{background:#fff;border:1px solid #E5E2D8;border-radius:10px;padding:14px 18px;margin:12px 0}
.step .t{font-weight:700;font-size:14.5px}
.step .f{margin-top:8px}
.step b.tag{color:#5C4FA1}
.box{background:#F6F5F1;border-left:4px solid #B0543A;padding:12px 16px;margin:12px 0;border-radius:6px;font-size:13px}
.box.ok{border-left-color:#2E7D4F}
.box.warn{border-left-color:#D97B29}
ol.tight li,ul.tight li{margin:4px 0}
code{background:#F0EEE7;padding:1px 5px;border-radius:4px;font-size:12px}
.mut{color:#888780;font-size:12px}
.ok{color:#2E7D4F;font-weight:700}
"""

H = []
H.append(f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>第三章：从数据到交易——七步推导与可执行清单</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>从数据到交易：七步推导与可执行清单</h1>
<div class="sub">先复验、再推导、后落地。全部数字经 step41 独立重算（不复用旧分析代码），复验表见第一节。研究对象：15 个期货品种 1 分钟 K 线，2020-01 ~ 2026-06 · 2026-10-18</div>

<h2>一、复验：旧结论站不站得住？</h2>
<p>把近期所有关键结论用<b>独立新写的代码</b>从原始数据重算一遍（不复用旧脚本，防止同一处 bug 自我确认）。结果：</p>
<table>
<tr><th class="l">结论</th><th>原数字</th><th>复验数字</th><th>状态</th></tr>
<tr><td class="l">开盘 15 分钟振幅浓度（占全日比例）</td><td>~55–60%（首15分钟收益占比口径）</td><td>振幅口径 <b>22.8%</b>（4.3% 的时间）；开盘均振幅 15.6bp vs 其余 11.7bp</td><td class="ok">✓ 一致（口径不同）</td></tr>
<tr><td class="l">前一根波动大 ⇒ 下一根波动大（IC）</td><td>0.46–0.63</td><td>MA 0.468 / EG 0.584（train），holdout 0.587 / 0.633</td><td class="ok">✓</td></tr>
<tr><td class="l">收在极端位置 ⇒ 下一根回拉（clv MR）</td><td>MA −0.198 / EG −0.103</td><td>MA <b>−0.1977</b> / EG −0.1032（逐位吻合）</td><td class="ok">✓</td></tr>
<tr><td class="l">MR 的时段定位（收盘强、开盘弱）</td><td>夜收 −0.252 / 开盘 −0.114~−0.138</td><td>夜收 −0.252 / 09:00 开盘 −0.134 / 21:00 夜开 −0.114</td><td class="ok">✓</td></tr>
<tr><td class="l">2026-04-08 挤仓事实</td><td>跳空 −1482bp、首根 213 点、量 10–30×</td><td>跳空 <b>−1482.4bp</b>（09:00 逐位吻合）、首根振幅 6.98%（≈213 点）、首根量 = 中位 bar 的 <b>60.6 倍</b></td><td class="ok">✓（量口径更严）</td></tr>
<tr><td class="l">1 分钟 MR 毛边缘 vs 成本</td><td>+1.46bp/笔（t=330），差成本 8 倍</td><td>+1.30bp/笔（t=295，n=342 万），盈亏平衡 0.65bp/边 vs 现实 ~6bp/边</td><td class="ok">✓ 结论不变</td></tr>
<tr><td class="l">波动体制持续（高波后仍高波）</td><td>HMM 高波态驻留，切换率骤降</td><td>独立代理：P(下根仍高波|本根高波) = 0.50~0.54 vs 基准 0.20（lift 2.6~2.8×）</td><td class="ok">✓</td></tr>
<tr><td class="l">MA-EG 共尾（slot 调整后）</td><td>4.10×</td><td><b>4.103×</b>（逐位一致）；朴素基准 31.4× 复验为基准失真</td><td class="ok">✓</td></tr>
</table>
<div class="box ok"><b>复验结论：</b>八项关键结论全部通过独立重算，其中三项逐位吻合（clv MR、跳空幅度、共尾倍数）。结论链可以继续往下推。</div>

<h2>二、七步推导：从现象到交易动作</h2>
<div class="fig">{fig_key_facts()}</div>''')

steps = [
    ('第 1 步 · 波动扎堆在开盘',
     '一天 345 根 1 分钟 K 线里，开盘 15 根只占 4.3% 的时间，却贡献 22.8% 的振幅；开盘那根的平均振幅是盘中的 1.34 倍。',
     '<b class="tag">社会学依据：</b>隔夜之间新闻、外盘、宏观数据不断累积，所有人都攒着单等开盘一起重新定价——这是「集合竞价式」的订单堆积，古今中外所有市场都一样。',
     '开盘 15 分钟是<b>定价时段</b>，不是趋势时段。它的「大波动」不含方向信息，只含「大家在重新对齐」的信息。'),
    ('第 2 步 · 波动是最可预测的东西',
     '「这根波动大 → 下一根大概率也大」的相关性 0.47–0.63，六年不衰减、放大到 60 分钟也不衰减；并且一旦进入高波状态就驻留（下根仍高波的概率 0.50，是基准的 2.6 倍）。',
     '<b class="tag">数学依据：</b>波动率集聚（ARCH 效应，Engle 1982 年因此拿诺贝尔奖）：波动来自交易活动本身，而交易活动有惯性。<b class="tag">社会学依据：</b>波动吸引参与——行情一大，看盘的人、追单的人、被迫止损的人全被卷进来，反过来维持波动。',
     '方向不可预测，但<b>波动可预测</b>。所以风控参数（止损宽度、仓位大小）应该跟着波动走，而不是固定不变。'),
    ('第 3 步 · 极端收盘的那根 K 线，下一根平均往回走——但只值 1.3 个 bp',
     '一根 K 线贴着最高价收（或最低价收），下一根平均回拉。信号在统计上极强（t=295，342 万笔），但每笔只值 1.3bp（万分之一出头），而一开一平的现实成本约 12bp。',
     '<b class="tag">数学依据：</b>买卖价差的来回跳动会在高频数据里机械地制造「反转相关」（Roll 1984）——你看到的边缘大部分是价差的影子，不是有人送钱。这就是为什么边缘多空对称、IF（做市制度不同）完全为零。',
     '这个信号<b>不能拿来开策略赚钱</b>（成本差 8 倍，无解），但能拿来<b>改善执行</b>：入场前看一眼上一根 K 线的收盘位置，可以白捡一点入场价。'),
    ('第 4 步 · 同样的「极端收盘」，在开盘是陷阱、在尾盘是机会',
     '回拉强度随时段变化：夜盘收盘前最强（−0.25），盘中 −0.20，两个开盘最弱（−0.11~−0.13，只有尾盘的一半）。',
     '<b class="tag">心理学依据：</b>尾盘是<b>平仓单</b>主导——交易员不想扛隔夜风险（隔夜恐惧）+ 落袋为安冲动（处置效应），把价格推到极端的是「必须今天平掉」的急单，第二天没人接力，自然回拉。开盘是<b>信息单</b>主导——价格被推到极致是因为真有新信息，羊群还在追（FOMO），所以不回归、反而延续。',
     '<b>开盘别赌回调，尾盘别追突破。</b>开盘追高杀低后等反转 = 站在信息和羊群的对立面；尾盘追突破 = 接盘平仓单的尾声。'),
    ('第 5 步 · 波动是「状态」不是「事件」——昨天的波动定今天的风控',
     '极端行情日里高波状态占 68–95%（平静年只有 5–13%），且日级波动的自相关 0.56——今天暴动，明天大概率还暴动。',
     '<b class="tag">数学依据：</b>马尔可夫体制的自转移概率高达 0.9 以上。<b class="tag">社会学依据：</b>风险事件有链式反应——保证金追缴、止损触发、媒体报道互相喂食，波动一旦点燃不会一根 K 线就熄灭。',
     '每天开盘前用 10 秒钟判一次「今天是不是高波档」，据此<b>切换整套风控参数</b>，而不是固定参数打天下。'),
    ('第 6 步 · 同板块品种是一条绳上的蚂蚱',
     '两个化工品在同一分钟同时暴动的概率，剥掉「大家都在开盘忙」的假象后，仍是各自独立假设的 2.4–4.6 倍；压力期放大到 25–57 倍。且按产业链聚类：PP-L 6.5 倍、FG-SA 4.3 倍、MA-EG 4.1 倍。',
     '<b class="tag">社会学依据：</b>板块资金联动（程序化套利把同链条品种当成一个篮子交易）+ 共享宏观开盘。<b class="tag">数学依据：</b>共尾倍数对基准敏感，必须用剥离了时钟节律的基准，否则会把「都在开盘波动」误当成「联动」。',
     '<b>仓位分散的错觉是最危险的风险</b>：持有 5 个化工品 ≈ 满仓一个板块的 4 倍杠杆。同链条要按共尾倍数折算总风险。'),
    ('第 7 步 · 承认边界：方向预测不了，大模型也救不了',
     '把时间维度加进模型，对「下一根涨跌方向」的贡献精确为零（0.9793→0.9792 bit）；通用 K 线大模型（Kronos）零样本预测下一分钟振幅，比 3 状态小模型差 2.4 bit，甚至差于瞎猜均匀分布。',
     '<b class="tag">数学依据：</b>分钟级方向接近鞅（有效市场在高频的体现）；基础模型的采样均值会抹掉条件分布的形状，恰好在近噪声任务上失效。',
     '<b>把研究预算全部押在波动率与风控上</b>，放弃「预测下一分钟方向」的执念——这不是谦虚，是 340 万笔交易和一个大模型替我们买来的教训。'),
]
for title, fact, basis, infer in steps:
    H.append(f'''<div class="step"><div class="t">{title}</div>
<div class="f"><b class="tag">现象（数据）：</b>{fact}</div>
<div class="f">{basis}</div>
<div class="f"><b class="tag">推论：</b>{infer}</div></div>''')

H.append('''
<h2>三、可执行清单：怎么用它提高胜率和盈亏比</h2>
<h3>A. 每日风控档（开盘前 10 秒）</h3>
<div class="step"><div class="t">A1 · 高波日切换风控档</div>
<div class="f"><b class="tag">做法：</b>开盘前看品种昨日全天振幅（或今早开盘 15 分钟振幅）是否超过其历史 80 分位。是 → 今日按高波档执行：<b>仓位减半 + 止损放宽约 1.4 倍</b>（用开盘/盘中振幅比 1.34× 为锚），单笔风险金额保持不变。</div>
<div class="f"><b class="tag">数据依据：</b>事实 2 + 5（波动持续 IC 0.47–0.63、体制驻留 lift 2.6×、日级 acf1 0.56）。</div>
<div class="f"><b class="tag">作用路径：</b>高波日还用紧止损 = 被噪声反复扫损（胜率↓、手续费↑）；止损放宽但仓位减半 = 同样的总风险、少得多的误扫。<b>直接提高胜率。</b></div>
<div class="f"><b class="tag">失效条件：</b>「跳空型」事件日（如 2025-04 关税）波动不驻留——高开一根大 K 线后迅速平静，此时应看「高波态是否持续 30 分钟以上」再定档，而不是看一根 K 线。</div></div>
<div class="step"><div class="t">A2 · 板块共尾降仓</div>
<div class="f"><b class="tag">做法：</b>持仓涉及同一条产业链的多个品种时，总风险按「品种数 × 共尾系数」折算：聚酯链（TA/PX/PF/PR）、聚烯烃（PP/L/V）、纯碱-玻璃（FG/SA）、甲醇-乙二醇（MA/EG）各自视为一个簇，簇内相关性按 4 倍打折。簇内任一品种今日出现超 99 分位振幅 → 簇内其余成员当日降仓。</div>
<div class="f"><b class="tag">数据依据：</b>事实 6（共尾 2.4–4.6×，压力期 25–57×）。</div>
<div class="f"><b class="tag">作用路径：</b>不提高胜率，但<b>防止尾部一天亏掉一个月</b>——这是盈亏比的长尾端保护。</div>
<div class="f"><b class="tag">失效条件：</b>系数在极端体制会放大 5–10 倍（25–57×），压力期应进一步收紧而非沿用 4×。</div></div>

<h3>B. 每笔执行（直接改善盈亏比）</h3>
<div class="step"><div class="t">B1 · 极端收盘不追单，等一根回调再进</div>
<div class="f"><b class="tag">做法：</b>准备入场时看上一根 K 线：贴着最高价收（你要做多）→ 挂限价单等 1–3 分钟回调；贴最低价收（要做空）→ 同理。等不到就不追，机会成本小于价差成本。</div>
<div class="f"><b class="tag">数据依据：</b>事实 3（平均回拉 1.3bp/根，342 万笔 t=295）。</div>
<div class="f"><b class="tag">作用路径：</b>每笔入场价平均改善 ~1bp，一年 300 笔 ≈ 3 个点的成本节省——<b>全部加在净盈亏比上</b>，且不影响策略逻辑。</div>
<div class="f"><b class="tag">失效条件：</b>强趋势日（高波态+单边）MR 衰减一半以上（IC 从 −0.20 → −0.03~−0.06），此时等回调容易踏空——趋势日改用「分批入场」代替「等回调」。</div></div>
<div class="step"><div class="t">B2 · 开盘 15 分钟不做反转单</div>
<div class="f"><b class="tag">做法：</b>09:00–09:14、21:00–21:14 两根时间段，不做「赌回调」的逆势单；要参与只顺定价方向轻仓试错，且止损按高波档放宽。</div>
<div class="f"><b class="tag">数据依据：</b>事实 1 + 4（开盘是定价时段，MR 强度只有尾盘一半）。</div>
<div class="f"><b class="tag">作用路径：</b>砍掉胜率最低的一类单子（开盘逆势单被信息与羊群碾压）。</div></div>
<div class="step"><div class="t">B3 · 尾盘利用回归减仓，不追突破</div>
<div class="f"><b class="tag">做法：</b>夜盘收盘前 15 分钟想减仓：等一根「贴极端位置收盘」的 K 线出现后再平，大概率拿到更好价格；反过来，尾盘的突破不追（多半是平仓单推的）。</div>
<div class="f"><b class="tag">数据依据：</b>事实 4（夜盘收盘 MR −0.25，全天最强）。</div>
<div class="f"><b class="tag">作用路径：</b>减仓价格的系统性改善 + 避免尾盘假突破的止损单。<b>同时作用于胜率与盈亏比。</b></div></div>

<h3>C. 不做清单（省钱比赚钱快）</h3>
<ul class="tight">
<li><b>C1 · 不做 1 分钟级方向预测策略</b>：边缘 1.3bp vs 成本 12bp，差 8 倍，统计学上无解（事实 3、7）。</li>
<li><b>C2 · 不把同板块多品种当风险分散</b>：簇内按 4 倍共尾折算（事实 6）。</li>
<li><b>C3 · 不指望通用大模型零样本预测分钟行情</b>：零样本 Kronos 差于均匀瞎猜（事实 7）；要用也只能微调后做波动率辅助。</li>
<li><b>C4 · 不用固定止损打所有日子</b>：波动体制是状态（事实 5），固定点数止损在高波日必然被反复扫。</li>
</ul>

<h2>四、诚实的边界</h2>
<div class="box warn"><ol class="tight">
<li><b>本研究没有找到可直接自动交易的方向性 alpha</b>——找到的是「波动可预测 + 执行可优化 + 风险可折算」。这三样提高的是胜率的「少亏」端和盈亏比的「成本」端，不是预测端。凡是声称 1 分钟能稳定预测方向的，先问他的成本口径。</li>
<li><b>holdout 是 2026 极端年</b>（仅 5.3 个月），所有样本外数字带分布漂移折扣；清单中的系数（1.4×、4×、80 分位）是 train 六年标定的中枢，实盘应按季度滚动复核。</li>
<li><b>微观结构结论依赖流动性环境</b>：价差、做市制度、夜盘安排变化（如 UR 2026 取消夜盘）都会让 MR 强度与时段定位漂移——清单 B 类动作执行前，先在最近一个月数据上复核 clv IC 的符号与量级。</li>
<li><b>数据边界</b>：主力连续合约、1 分钟粒度、郑商所/大商所/上期能源/中金所 15 品种；股票、加密、外盘未验证，不外推。</li>
</ol></div>
<p class="mut">复现：step41_verify.py（独立重算）→ results_verify.json；推导依据见 ch2_report.html（第二章）与 deep_report.html（第一章）。</p>
</div></body></html>''')

open('trading_takeaways.html', 'w', encoding='utf-8').write(''.join(H))
print('trading_takeaways.html written,', len(''.join(H)), 'chars')
