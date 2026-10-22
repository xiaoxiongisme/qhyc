# -*- coding: utf-8 -*-
"""step47 生产库 56 品种全量回测报告（results_step46.json → fusion_prod_report.html）"""
import json
import numpy as np
import pandas as pd

R = json.load(open('results_step46.json'))

CSS = '''body{font-family:"Microsoft YaHei",sans-serif;max-width:1080px;margin:0 auto;padding:24px;color:#2B2B2B;background:#FDFDFB;line-height:1.75}
h1{font-size:24px;border-bottom:3px solid #C53D43;padding-bottom:8px}
h2{font-size:19px;margin-top:36px;border-left:5px solid #C53D43;padding-left:10px}
h3{font-size:16px;margin-top:24px;color:#8C2F39}
table{border-collapse:collapse;margin:10px 0;font-size:12.5px;width:100%}
td,th{border:1px solid #DDD;padding:4px 8px;text-align:right}
td.l,th.l{text-align:left}
tr:nth-child(even){background:#FAFAF7}
tr.hl{background:#FFF7E8;font-weight:600}
.box{background:#F5F8FC;border-left:4px solid #3E6FB0;padding:10px 16px;margin:12px 0;font-size:14px}
.box.good{background:#F4FAF6;border-color:#3E9B6A}
.box.bad{background:#FDF3F2;border-color:#C53D43}
.box.warn{background:#FDF9F0;border-color:#C9862B}
.mut{color:#8A8578;font-size:12px}
.g{color:#2E7D4F;font-weight:600}.r{color:#C53D43;font-weight:600}
code{background:#F0EEE8;padding:1px 5px;border-radius:3px;font-size:12px}
ul.tight li,ol.tight li{margin:4px 0}
'''


def wan(x):
    return f'{x / 1e4:+.1f}'


def row(v, hl=False, note=''):
    m = R[v]
    ho = m.get('holdout', {})
    tr = m.get('train', {})
    cls = ' class="hl"' if hl else ''
    return (f'<tr{cls}><td class="l"><b>{v}</b>{("　" + note) if note else ""}</td><td>{m["n"]:,}</td>'
            f'<td>{m.get("win", 0) * 100:.1f}%</td><td>{m.get("pf", np.nan):.3f}</td>'
            f'<td class="{"g" if m.get("net_yuan", 0) > 0 else "r"}">{wan(m.get("net_yuan", 0))}</td>'
            f'<td class="r">{wan(m.get("mdd_yuan", 0))}</td>'
            f'<td>{m.get("mar", np.nan):.2f}</td>'
            f'<td class="{"g" if tr.get("net_yuan", 0) > 0 else "r"}">{wan(tr.get("net_yuan", 0))}</td>'
            f'<td class="{"g" if ho.get("net_yuan", 0) > 0 else "r"}">{wan(ho.get("net_yuan", 0))}</td>'
            f'<td>{ho.get("pf", np.nan):.2f}</td></tr>')


THEAD = ('<table><tr><th class="l">变体</th><th>笔数</th><th>胜率</th><th>PF</th>'
         '<th>净利(万)</th><th>回撤(万)</th><th>MAR</th>'
         '<th>train(≤25)</th><th>holdout(26)</th><th>ho PF</th></tr>')

H = [f'<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>']
H.append('''<h1>融合策略 × 生产库 56 品种：V4-RC 复验 + MACD/KDJ/量价/持仓因子融合验证</h1>
<p class="mut">数据：云端生产库 qhyc-timescaledb（2026-10-18 导出）：l2_adj.bar_60m_ba 后复权原生 60 分棒 · l0_raw.daily_bar · dim_trading_cost 真实费率字典 · l2_adj.factor_value（member_net_z，available_at 严格滞后）｜引擎：step46_prod_bt.py 逐行移植 fusion_signal.py walk_fusion_states（V3.4 定稿）｜未改动生产环境</p>''')

base = R['BASE']
H.append(f'''<h2>〇、一句话结论</h2>
<div class="box good"><b>三个可采纳项（生产库全品种 + 真实成本 + 反向对照全过）：</b><br>
① <b>M5 会员净持仓门 |z|≥1.0</b>：净利 {wan(base["net_yuan"])} → <b class="g">{wan(R["M5"]["net_yuan"])}</b>（×2.9），回撤 {wan(base["mdd_yuan"])} → {wan(R["M5"]["mdd_yuan"])}（收窄 46%），MAR 0.88 → <b>4.67</b>；反向对照 −49.2 万，正反差 <b>+197.6 万</b>；剔 TOP1 品种仍 +46.1 万（BASE 剔 TOP1 为 −30.2 万）。<br>
② <b>M1a MACD 柱同号门</b>（回踩支路 hist 必须与方向同号）：净利 → <b class="g">{wan(R["M1a_pb"]["net_yuan"])}</b>，MAR 2.33；反向对照 −1.6 万，正反差 <b>+108 万</b>；train/holdout 同向改善（+50.3 / +56.4 万）。<br>
③ <b>P6 尾盘根不开仓</b>：净利 → <b class="g">{wan(R["P6"]["net_yuan"])}</b>，MAR 1.72，train/holdout 同向（+50.3 / +44.1 万）。<br>
<b>终版组合 V5X = P2+P4+P6+M1a+M5</b>：净利 <b class="g">{wan(R["V5X"]["net_yuan"])}</b>、PF 1.223、MAR 2.25、分年 9/12 为正、剔 TOP1 转正（+35.8 万）。</div>''')

H.append(f'''<h2>一、口径升级说明（与 15 品种版的四处差异）</h2>
<div class="box"><ol class="tight">
<li><b>品种池</b>：15 化工+IF → <b>56 个商品品种</b>（59 只有数据；剔 CJ 红枣【日线复权损坏】与无费率品种；金融期货 IF/IC/IH 不在生产回测池）。<b>注意：旧版利润引擎 IF 不在池内</b>，且 AU（黄金）成为最大单品贡献（BASE 里 AU 一只 +82.2 万）。</li>
<li><b>K 线</b>：1m 聚合小时桶 → <b>原生 60 分棒</b>（官方分桶，含会话末微量 stub 棒；消除 10:15 休市口径误差）。</li>
<li><b>成本</b>：统一 1.5bp+1tick → <b>真实费率字典</b>（FIXED/PCT/FREE × OPEN/CLOSE_TODAY/CLOSE_YEST + slip_ticks×tick；SC/AU 等平今免费生效）。</li>
<li><b>日历与预热</b>：官方交易日历（夜盘归次日）+ 上市 200 自然日预热，对齐生产 sym_cut。</li>
</ol>
<p>BASE 在生产库全品种口径下：{base["n"]:,} 笔、胜率 22.1%、PF 1.026、净利 {wan(base["net_yuan"])} 万、回撤 {wan(base["mdd_yuan"])} 万、MAR 0.88。
<b>本引擎为「固定 1 手 × 逐品种独立」口径</b>（无组合层 max_products/保证金上限/评分排序），绝对值不可与生产组合回测锚点（109.7/103.9 万）直接比；价值在变体间相对比较。</p></div>''')

H.append(f'''<h2>二、V4-RC 四项在 56 品种上的复验（关键：簇控翻案）</h2>
{THEAD}''')
for v, nt in [('BASE', '基线'), ('P2', '高波日歇'), ('P3', '簇控≤2+冲击封禁'), ('P4', '化工只回踩'),
              ('P4E', '全能化只回踩'), ('P6', '尾盘根不开仓'), ('V4NP2', 'P2+P3+P4+P6 含簇控'),
              ('V4X', 'P2+P4+P6 不含簇控')]:
    H.append(row(v, hl=(v == 'V4X'), note=nt))
H.append(f'''</table>
<div class="box bad"><b>翻案：P3 簇控（簇内同时持仓 ≤2 + 冲击日封禁）在全品种池上有害，否决。</b>
修复一个静默 bug 后（trade 的 sym 是 'MA888' 而簇表键是 'MA'，首轮 A2 实际未生效——P3 与 BASE 逐位相同就是指纹）：
P3 单独 {wan(R["P3"]["net_yuan"])} 万（BASE {wan(base["net_yuan"])} 万），含簇控的组合全面劣于不含簇控（V4NP2 {wan(R["V4NP2"]["net_yuan"])} vs V4X {wan(R["V4X"]["net_yuan"])} 万）。
原因解读：簇控拦掉的恰恰包括 AU/J 等「同簇龙头接续行情」的盈利单；15 品种版里它为正、56 品种版为负——<b>簇控的收益方向依赖品种池构成，不是普适规则</b>。第二章「簇内共尾 4×」的风控含义保留（折算风险敞口），但作为入场过滤器否决。</div>
<div class="box warn"><b>P2（高波日歇）在全品种池上也明显弱于 15 品种版</b>：总贡献仅 {wan(R["P2"]["net_yuan"] - base["net_yuan"])} 万，且 holdout 比 BASE 差（8.3 vs 38.3 万）。
15 品种版 +65 万主要来自化工+IF 的高波特征；全市场口径下高波日过滤会把 2026 年贵金属/黑色的大行情也拦掉。<b>P2 降级为「可选模块」，不进终版默认组合。</b></div>
<div class="box good"><b>P4（化工只回踩）与 P6（尾盘根不开仓）复验通过</b>：P4 {wan(R["P4"]["net_yuan"])} 万（holdout 42.6 万改善）、P6 {wan(R["P6"]["net_yuan"])} 万（单项第二强）；P4E（扩展到全能化）反而回落 → 维持原边界「只有聚酯/聚烯烃/两碱/醇醚四链」。</div>''')

H.append(f'''<h2>三、MACD / KDJ / 量价 / 持仓因子门控（单项 × 反向对照）</h2>
{THEAD}''')
for v, nt in [('M1a_pb', 'MACD柱同号·仅回踩'), ('M1a_all', 'MACD柱同号·全支路'),
              ('M1b', '金叉遇绿柱精确版'), ('M2', 'K>80禁突破多'),
              ('M3', '放量突破门≥1.5×'), ('M5', '会员|z|≥1.0'), ('M5b', '会员|z|≥1.0+方向'),
              ('M1a_rev', '⚠反向对照'), ('M2_rev', '⚠反向对照'), ('M5_rev', '⚠反向对照')]:
    H.append(row(v, hl=(v in ('M1a_pb', 'M5')), note=nt))
H.append('''</table>
<table><tr><th class="l">门控</th><th>正向净利(万)</th><th>反向净利(万)</th><th>正反差(万)</th><th class="l">裁决</th></tr>''')
for fwd, rev, nm, verdict in [
        ('M1a_pb', 'M1a_rev', 'M1a MACD 柱同号', '<span class="g">采纳</span>（反向 −1.6 ≈ 0，正反差 +108 万）'),
        ('M2', 'M2_rev', 'M2 KDJ 位置门', '<span class="r">否决</span>（反向 +59.9 也为正 ⇒ 只是「少做单」，无方向信息）'),
        ('M5', 'M5_rev', 'M5 会员净持仓', '<span class="g">采纳</span>（反向 −49.2 亏损，正反差 +197.6 万，复验前次报告）')]:
    f, rv = R[fwd]['net_yuan'], R[rev]['net_yuan']
    H.append(f'<tr><td class="l">{nm}</td><td>{wan(f)}</td><td>{wan(rv)}</td>'
             f'<td class="g">{wan(f - rv)}</td><td class="l">{verdict}</td></tr>')
H.append(f'''</table>
<div class="box"><b>逐条证据链与实测裁决：</b>
<ul class="tight">
<li><b>M1a（MACD 柱同号）✅ 采纳</b>：依据 59 品种报告最稳的两条——「金叉遇绿柱 −12.5pp（48/49 品种、三周期、12 分年全负）」「死叉遇红柱 +12.6pp（49/49）」。回踩支路本质是「逆势反弹接回顺势」，hist 同号门正好砍掉动量最弱的一批：净利 ×2.05、MAR 2.33、holdout PF 1.24。<b>仅回踩支路（pb）版优于全支路版</b>（106.6 vs 99.4 万），突破支路自带动量确认、不必再门。</li>
<li><b>M1b（精确事件版）❌ 否决</b>：3 根内 KDJ 金叉+绿柱才禁——事件太稀（只拦 8% 信号），holdout 转负（−4.8 万）。<b>教训：证据是「事件研究」口径的，搬成「逐根门控」会失真；用连续状态（hist 符号）而非离散事件（3 根内金叉）。</b></li>
<li><b>M2（KDJ 位置门）❌ 否决</b>：虽然表面 +96.3 万，但反向对照也是正的（+59.9 万）——两个方向都赚钱说明它不含方向信息，只是恰好拦了些差单。与 59 品种复核「高位金叉归零」一致。</li>
<li><b>M3（放量突破门）❌ 否决</b>：{wan(R["M3"]["net_yuan"])} 万，比 BASE 还差。<b>放量不是真突破的确认而是噪声放大器</b>——60 分棒级别的放量与突破质量无关。量价维度在本策略时间尺度上无可交易信号（与波段统计「成交量集中度 IC 0.055 未验证」一致）。</li>
<li><b>M5（会员净持仓 |z|≥1.0）✅ 采纳（全场最强）</b>：净利 ×2.85、回撤收窄 46%、MAR 4.67、反向对照亏 −49.2 万、剔 TOP1 转正。<b>阈值敏感性复核旧结论成立</b>：|z|≥0.5 → {wan(R["M5_05"]["net_yuan"])} 万 / ≥1.0 → {wan(R["M5"]["net_yuan"])} 万 / ≥1.5 → {wan(R["M5_15"]["net_yuan"])} 万——<b>1.0 就是甜点，不要再加</b>。M5b（加方向一致）总净利更高（+165.7 万）但 holdout 仅 +9.7 万（方向条件在 2026 极端年反噬），<b>推荐默认只用强度门 M5</b>。</li>
</ul></div>''')

H.append(f'''<h2>四、组合终版对比总表</h2>
{THEAD}''')
for v, nt in [('BASE', '基线'), ('V4X', 'P2+P4+P6'), ('V4X_M1', '+MACD门'), ('V4X_M5', '+会员门'),
              ('V5X', '全项（终版候选）'), ('V5', '含簇控+放量（反例）')]:
    H.append(row(v, hl=(v == 'V5X'), note=nt))
H.append('</table>')
df5 = pd.read_csv('trades46_V5X.csv')
years = ' '.join(f'{y}:{g.net.sum() / 1e4:+.0f}' for y, g in df5.groupby('year'))
ps = df5.groupby('sym').net.sum().sort_values(ascending=False)
H.append(f'''<div class="box good"><b>终版 V5X = 高波日歇（P2）+ 化工只回踩（P4）+ 尾盘根不开仓（P6）+ MACD 柱同号门（M1a·回踩支路）+ 会员净持仓 |z|≥1.0（M5）</b><br>
净利 <b>{wan(R["V5X"]["net_yuan"])} 万</b>（BASE 的 2.6 倍）、PF 1.223、胜率 24.3%、MAR 2.25、6,744 笔。<br>
分年（万）：{years} —— 9/12 年为正。<br>
集中度：剔 TOP1（AU）后 +35.8 万（BASE 为 −30.2 万，<b>由负转正</b>）；剔 TOP3 仍 −34.2 万（「靠大行情吃饭」的本质不变，但脆弱性显著下降）。<br>
train(≤2025) {wan(R["V5X"]["train"]["net_yuan"])} 万 / holdout(2026) {wan(R["V5X"]["holdout"]["net_yuan"])} 万（PF 1.22）——两期同号。</div>
<div class="box warn"><b>反例对照：V5（含簇控+放量门的「全家桶」）反而只有 {wan(R["V5"]["net_yuan"])} 万、holdout −3.6 万。</b>每一条「听起来合理」的过滤叠加都在消耗样本与 edge——过滤器的边际效用递减且会互相踩脚（M3 拦的突破单里有 M5 放行的盈利单）。<b>终版只保留过了反向对照的门。</b></div>''')

H.append('''<h2>五、维度数据 × MACD × KDJ × 量价 × 持仓 的配合逻辑（定稿设计图）</h2>
<table>
<tr><th class="l">层级</th><th class="l">回答的问题</th><th class="l">工具（定稿）</th><th class="l">证据</th></tr>
<tr><td class="l">① 体制层（今天做不做）</td><td class="l">市场环境</td><td class="l">P2 高波日歇（可选模块，化工池强/全市场弱）</td><td class="l">★★ 池依赖</td></tr>
<tr><td class="l">② 方向层（做哪侧）</td><td class="l">多空许可</td><td class="l">小时 EMA140（生产口径，不动）</td><td class="l">★★★ 已固化</td></tr>
<tr><td class="l">③ 持仓确认层（主力站哪边）</td><td class="l">资金面确认</td><td class="l"><b>M5 会员净持仓 |z|≥1.0</b>（T+1 严格滞后）</td><td class="l">★★★ 两轮独立复验+反向对照</td></tr>
<tr><td class="l">④ 动量确认层（动量同不同向）</td><td class="l">价格动了，动量跟没跟</td><td class="l"><b>M1a MACD 柱同号（仅回踩支路）</b></td><td class="l">★★★ 59 品种铁律 + 本章反向对照</td></tr>
<tr><td class="l">⑤ 触发层（哪里进）</td><td class="l">回踩 / 突破</td><td class="l">MA20 回踩+斐波汇流；化工只回踩（P4）；突破不加量门</td><td class="l">★★★ / 量门★（否决）</td></tr>
<tr><td class="l">⑥ 时段层（哪根不进）</td><td class="l">尾盘根不做</td><td class="l">P6（clv 尾盘回归的反向利用）</td><td class="l">★★★ 两池复验</td></tr>
<tr><td class="l">⑦ 离场层</td><td class="l">2ATR+吊灯+保本</td><td class="l">不动（历史所有离场改动均被否决）</td><td class="l">★★★ 不改即结论</td></tr>
</table>
<div class="box"><b>KDJ 的最终定位：不直接进门控。</b>它在本策略里的价值是「解释变量」而非「过滤变量」——金叉 86~92% 是事后补票、位置门无方向信息、宽共振有未来信息陷阱。KDJ 研究的最大产出是<b>逼出了 MACD 柱同号这条铁律</b>（金叉遇绿柱的反面）。</div>''')

H.append('''<h2>六、口径与边界声明</h2>
<ul class="tight">
<li>引擎为「固定 1 手 × 逐品种独立」口径，<b>无组合层</b>（生产有 max_products=10 / 保证金上限 / 评分排序）；终版 6,744 笔/12 年 ≈ 每天 2.3 笔信号，组合层截断会进一步改变结果——上线前须用生产组合引擎复核。</li>
<li>member_net_z 因子当前覆盖 56 品种、最新至 2026-09-30（导出时）；缺数据的品种/日期<b>放行</b>（不因缺数据弃单），与上轮报告口径一致。</li>
<li>2026 holdout 是极端体制样本（第二章）；V5X holdout +19.2 万但绝对值小于 train 年化，勿线性外推。</li>
<li>剔 TOP3 仍为负：策略本质靠大行情品种吃饭，门控降低脆弱性但不改变结构。</li>
<li>本轮回测未改动生产环境任何文件；数据源为 2026-10-18 只读导出。</li>
</ul>
<p class="mut">生成：step47_prod_report.py ｜ results_step46.json（26 个变体）｜ trades46_*.csv ｜ 2026-10-18</p></body></html>''')
open('fusion_prod_report.html', 'w', encoding='utf-8').write('\n'.join(H))
print('saved fusion_prod_report.html')
