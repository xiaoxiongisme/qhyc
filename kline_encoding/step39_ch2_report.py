# -*- coding: utf-8 -*-
"""Step 39: 组装第二章 HTML 报告（板块扩展 · 极端行情 2020/2026 · 可交易性 · Kronos 基准）"""
import json
import numpy as np
import step39_figs as F

EXT, SEC, CHN, HMM = F.EXT, F.SEC, F.CHN, F.HMM
BT, BTD, GAP, KRO = json.load(open('results_backtest.json')), F.BTD, json.load(open('results_gap.json')), F.KRO
TD = F.TD
SYMS12 = ['MA', 'EG', 'TA', 'PP', 'L', 'V', 'UR', 'FG', 'SA', 'PF', 'PX', 'PR']
SYMS15 = SYMS12 + ['EB', 'SC', 'IF']
WINS = ['CRASH20', 'BOUNCE20', 'BULL21', 'TARIFF25', 'SQUEEZE26', 'CALM24', 'TRAIN']
WINLAB = {'CRASH20': '2020 崩盘<br>(02-03~03-31)', 'BOUNCE20': '2020 反弹<br>(04-01~04-30)',
          'BULL21': '2021 牛<br>(09-01~11-30)', 'TARIFF25': '2025 关税<br>(04-01~04-30)',
          'SQUEEZE26': '2026 挤仓<br>(03-01~04-30)', 'CALM24': '2024 平静<br>(全年)', 'TRAIN': '训练集<br>(2020-2025)'}

CSS = """
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#FBFAF7;color:#26215C;margin:0;padding:32px 40px;line-height:1.65}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:24px;font-weight:700;margin:0 0 4px}
h2{font-size:18px;font-weight:700;margin:38px 0 10px;padding-left:10px;border-left:4px solid #5C4FA1}
h3{font-size:14.5px;font-weight:700;margin:22px 0 8px}
h4{font-size:13px;font-weight:700;margin:16px 0 6px;color:#5C4FA1}
.sub{color:#888780;font-size:12.5px;margin-bottom:18px}
.box{background:#fff;border:1px solid #E5E2D8;border-radius:10px;padding:14px 16px;margin:12px 0;font-size:13px}
.box.ok{border-left:4px solid #2E7D4F;background:#F4FAF6}
.box.warn{border-left:4px solid #EF9F27;background:#FDF9F0}
.box.bad{border-left:4px solid #C4372E;background:#FBF4F3}
.box.info{border-left:4px solid #5C4FA1;background:#F6F4FB}
table{border-collapse:collapse;width:100%;font-size:12px;margin:10px 0;background:#fff}
th{background:#F6F4EE;color:#26215C;font-weight:600;padding:6px 8px;border:1px solid #E5E2D8;text-align:center}
td{padding:5px 8px;border:1px solid #EFEDE4;text-align:center}
td.l{text-align:left}
tr:nth-child(even) td{background:#FCFBF8}
.fig{background:#fff;border:1px solid #E5E2D8;border-radius:10px;padding:10px;margin:14px 0}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.p-pass{background:#E4F2E9;color:#2E7D4F}.p-fail{background:#F9E7E5;color:#C4372E}.p-mid{background:#FBF0DC;color:#9A6209}
code{background:#F1EFE7;border-radius:4px;padding:1px 5px;font-size:11.5px}
.mut{color:#888780;font-size:11.5px}
b.r{color:#C4372E}b.g{color:#2E7D4F}b.p{color:#5C4FA1}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.grid2{display:block}}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
.kpi .c{background:#fff;border:1px solid #E5E2D8;border-radius:10px;padding:10px 12px}
.kpi .v{font-size:19px;font-weight:700;color:#5C4FA1}
.kpi .k{font-size:11px;color:#888780;margin-top:2px}
@media(max-width:800px){.kpi{grid-template-columns:1fr 1fr}}
ol.tight li,ul.tight li{margin:3px 0}
"""

H = []
H.append(f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>第二章：板块扩展 · 极端行情（2020/2026）· 可交易性 · 基础模型基准</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>第二章：板块扩展 · 极端行情 · 可交易性 · 基础模型基准</h1>
<div class="sub">15 品种（化工 12 + 原油 SC + 股指 IF）· 1 分钟 · 2020-01 ~ 2026-06-12 · train 至 2025（489,975 根/品种）· holdout 2026-01-05~06-12（35,865 根/品种，5.3 个月）· 口径：夜盘归属次日交易日、会话内相邻对 · 2026-10-18</div>

<h2>〇、四方向结论速览</h2>
<div class="kpi">
<div class="c"><div class="v">2.36×</div><div class="k">化工共尾均值（slot 调整 · train）<br>holdout 15.63×</div></div>
<div class="c"><div class="v">68–95%</div><div class="k">持续型极端窗口高波态占比<br>跳空型事件仅 9–23% · 平静年 5–13%</div></div>
<div class="c"><div class="v">+1.46bp</div><div class="k">池化毛边缘/笔（t=329.7）<br>盈亏平衡 0.73bp/边</div></div>
<div class="c"><div class="v">4.0–4.3 vs 1.7</div><div class="k">Kronos 零样本 nll vs HMM<br>MA/EG 均 N=200 · 同任务同口径（bit/根）</div></div>
</div>
<div class="box info"><b>本章四个方向全部完成——三个「推翻前论」+ 一个新实验（时间维度）：</b>
<ol class="tight">
<li><b>共尾不是均匀的板块现象，而是有产业链结构的</b>：slot 调整后化工两两共尾 1.27–6.52×，且按产业链聚类（聚烯烃 PP-L 6.52、纯碱-玻璃 FG-SA 4.25、甲醇链 MA-EG 4.10）；此前只用 MA/EG 两品种看到的 4.10× 是板块均值而非特例。</li>
<li><b>极端行情要分「持续型」与「跳空型」——前者是状态，后者只是事件</b>：2020 崩盘/反弹、2021 牛、2026 挤仓四个持续型窗口中，HMM 高波态占比 68–95%（平静年 5–13%）；而 2025-04 关税崩盘只有 MA 22.6% / EG 8.8%——一次 −652bp 的跳空之后市场迅速回归平静。<b>「出现极端 K 线」与「进入极端体制」是两件事</b>，前者不改变状态分布，后者才改变。</li>
<li><b>1 分钟 clv 均值回归信号在数学上显著、在实盘上不可交易</b>：池化毛边缘 +1.46bp/笔（t=329.7）但盈亏平衡成本仅 0.73bp/边，现实每边 ~6bp，差 8 倍；15 品种净收益全部 ≈ −10bp/笔 ≈ 成本本身。</li>
<li><b>时间维度有真实但高度集中的增益</b>：slot 单条件 +0.02~0.04 bit/根，其中开盘两个 15 分钟块 +0.49~+0.76 bit、其余时段 ≤0；叠加到「前波幅+链」模型之上只剩 +0.003~0.005 bit，方向任务 slot 增益 ≈ 0（见第五章）。</li>
</ol></div>

<h2>一、板块扩展：从 2 个品种到 15 个</h2>

<h3>1.1 数据扩展与口径</h3>
<p>从本地行情包提取 13 个新品种的主力连续 1 分钟数据（TA/PF/PX/PR/SA/FG/UR/PP/L/V/EB/IF/SC），与既有 MA/EG 合并为同构三段文件（2020 / 2021_2025 / 2026）。新品种的微观结构差异必须显式处理，否则会把口径差异误读成市场规律：</p>
<table>
<tr><th>品种</th><th>板块</th><th class="l">交易所 / 特色</th><th>首个交易日</th><th>2020</th><th>2021-25</th><th>2026</th></tr>
<tr><td>MA 甲醇</td><td>煤化工</td><td class="l">郑商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>EG 乙二醇</td><td>煤化工</td><td class="l">大商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>TA PTA</td><td>聚酯</td><td class="l">郑商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>PF 短纤</td><td>聚酯</td><td class="l">郑商所 · 2020-10 上市</td><td>2020-10-12</td><td>20,235</td><td>414,180</td><td>35,865</td></tr>
<tr><td>PX 对二甲苯</td><td>聚酯</td><td class="l">郑商所 · 2023-09 上市</td><td>2023-09-15</td><td>0</td><td>189,675</td><td>35,865</td></tr>
<tr><td>PR 瓶片</td><td>聚酯</td><td class="l">郑商所 · 2024-08 上市</td><td>2024-08-30</td><td>0</td><td>110,700</td><td>35,865</td></tr>
<tr><td>PP 聚丙烯</td><td>聚烯烃</td><td class="l">大商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>L 塑料</td><td>聚烯烃</td><td class="l">大商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>V PVC</td><td>聚烯烃</td><td class="l">大商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>UR 尿素</td><td>煤化工</td><td class="l">郑商所 · <b>2026 起无夜盘</b></td><td>2020-01-02</td><td>54,675</td><td>272,700</td><td>23,625</td></tr>
<tr><td>FG 玻璃</td><td>纯碱-玻璃</td><td class="l">郑商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>SA 纯碱</td><td>纯碱-玻璃</td><td class="l">郑商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>EB 苯乙烯</td><td>油化工</td><td class="l">大商所</td><td>2020-01-02</td><td>75,795</td><td>414,180</td><td>35,865</td></tr>
<tr><td>SC 原油</td><td>能源</td><td class="l">上期能源 · <b>夜盘至 02:29</b></td><td>2020-01-02</td><td>112,755</td><td>661,770</td><td>57,285</td></tr>
<tr><td>IF 沪深300</td><td>股指</td><td class="l">中金所 · <b>无夜盘 09:30 开盘</b></td><td>2020-01-02</td><td>58,320</td><td>290,880</td><td>25,200</td></tr>
</table>
<p class="mut">bar 数差异 = 会话结构差异（夜盘有无/长短），不是数据缺失：UR 2026 无夜盘、IF 09:30 开盘、SC 夜盘至次日 02:29。所有完整性审计（重复 bar、OHLC 一致性、会话长度精确性、换月指纹）在 15 品种上同样零污染通过。</p>

<h3>1.2 共尾矩阵：产业链结构浮出水面</h3>
<p>对全部 105 个品种对计算「同一分钟双双突破各自 train 99 分位振幅」的联合概率，用<b>分钟桶 slot 独立期望</b>做基准（剥离开盘/夜盘等季节性节律）。把矩阵按产业链重排后，对角线区块清晰可见：</p>
<div class="fig">{F.f1()}</div>
<div class="box"><b>读数：</b>化工两两 slot 调整共尾 <b>mean 2.36×</b>（区间 1.27–6.52），holdout（含 2026 挤仓）mean <b>15.63×</b>。最强的三对全部在同一条产业链上：
<b>PP-L 6.52×</b>（聚烯烃，丙烯-塑料同一原料端）、<b>FG-SA 4.25×</b>（纯碱-玻璃，玻璃窑炉的原料-产品关系）、<b>MA-EG 4.10×</b>（甲醇-乙二醇，MEG 的甲醇法原料关系）。
振幅相关性给出同一聚类的独立证据（slot 去均值后）：PP-L 0.781 / TA-PX 0.749 / PP-V 0.636 / FG-SA 0.526 / MA-EG 0.522。</div>
<div class="box warn"><b>IF 的反常：钟表错位剥开后共尾反而最高。</b>PX-IF 7.18×、PR-IF 9.16×、FG-IF 5.78×、SA-IF 4.65×、SC-IF 4.94×——股指与商品的共尾<b>高于任何两个商品之间</b>。朴素（非 slot）基准下这些配对被严重低估，因为 IF 09:30 开盘、商品 09:00/21:00 开盘，日内节律错位制造了虚假的「不共尾」；用分钟桶 slot 基准剥开钟表后，真实的共享宏观信息流（同一批交易者在开盘前 30 分钟对宏观数据的反应）才暴露出来。<b>方法学含义：共尾度量对基准极其敏感，「IF 与商品不共尾」是用错基准会得到的错误结论。</b></div>
<div class="box"><b>同向 vs 反向：</b>同向突破共尾 4.71× vs 反向 3.40×（比值 1.39）——板块共尾主要是<b>同向</b>的（共同订单流、共同宏观冲击），反向（对冲盘、跨品种套利）贡献约 72%。</div>

<h3>1.3 编码同质性：转移矩阵的相似聚类 ≠ 产业链聚类</h3>
<p>比较 15 品种两两的 8 态转移矩阵（逐行 TV 距离之和，同品种半分噪声底 mean 0.080 / sd 0.024）：</p>
<table>
<tr><th class="l">比较</th><th>TV 中位数</th><th>最小</th><th>最大</th><th class="l">解读</th></tr>
<tr><td class="l">化工两两（91 对）</td><td>0.888</td><td>0.156（MA-TA）</td><td>3.070（PR-EB）</td><td class="l">中位数 ≈ 噪声底 11 倍：链不同但都远离噪声</td></tr>
<tr><td class="l">四条链内部（13 对）</td><td>0.846</td><td>0.233（PX-PF）</td><td>1.817（TA-PR）</td><td class="l">与链间（0.831）几乎无差——<b>转移矩阵没有产业链结构</b></td></tr>
<tr><td class="l">IF 配对（14 对）</td><td>2.866</td><td>1.151</td><td>3.991（PR-IF）</td><td class="l">清晰离群：股指 8 态链与化工完全不同（开盘跳空主导其微观结构）</td></tr>
</table>
<div class="box warn"><b>一个必须如实报告的细节：</b>共尾矩阵里的产业链聚类（PP-L / FG-SA / MA-EG）<b>没有</b>出现在转移矩阵同质性里——链内 TV 0.846 vs 链间 0.831，差异在噪声范围内；而 TV 最低的配对反而是跨链的 MA-TA（0.156）、MA-SA（0.288）、EG-L（0.441）。两者度量不同的东西：共尾 = 极端振幅的<b>同步性</b>（订单流层面），转移矩阵 = 微观编码的<b>动力学相似性</b>（交易机制层面）。PR 上市仅两年、PX 不足三年，其转移矩阵与老品种差异大（TA-PR 1.817、PF-PR 1.521），部分是历史长度造成的估计差异，解读时需谨慎。</div>

<h3>1.4 链 vs 边际 vs HMM：隐状态模型是终审赢家</h3>
<p>对 15 个品种（化工 12 + 原油 SC + 股指 IF）做下一根振幅五档预测（holdout，log-loss bit/根）：边际（自身历史分布）≈ 2.32（≈均匀基线 log₂5=2.322）；一阶链（8 态转移）12/15 有小增益（V/IF 可忽略 &lt;0.01 bit，有效增益 10/15），二阶链在 12/15 上继续小幅改善；HMM（K=3，连续振幅发射）<b>14/15 品种大胜</b>，增益 0.135–0.829 bit（中位 0.538）：</p>
<div class="fig">{F.f5()}</div>
<table>
<tr><th>品种</th><th>HMM nll</th><th>HMM top-1</th><th>一阶链</th><th>二阶链</th><th>边际</th><th>HMM 增益<br>vs 边际</th></tr>''')
for s in sorted(CHN.keys(), key=lambda k: HMM[k]['hmm_nll']):
    gain = HMM[s]['chain_marg'] - HMM[s]['hmm_nll']
    gtxt = f'+{gain:.3f}' if gain >= 0 else f'−{abs(gain):.3f}'
    cls = ' style="background:#F4FAF6;font-weight:600"' if gain > 0.1 else (' style="background:#FDF9F0"' if gain <= 0 else '')
    H.append(f'<tr{cls}><td>{s}</td><td>{HMM[s]["hmm_nll"]:.3f}</td><td>{HMM[s]["hmm_top1"]:.3f}</td>'
             f'<td>{CHN[s]["m1"]:.3f}</td><td>{CHN[s]["m2"]:.3f}</td><td>{CHN[s]["marg"]:.3f}</td><td>{gtxt}</td></tr>')
H.append(f'''</table>
<div class="box warn"><b>两个必须解释的例外（都不是模型失败）：</b>
<ul class="tight">
<li><b>UR 输给链</b>（HMM 2.335 vs 一阶链 2.261）：UR 2026 年起取消夜盘，holdout 的分钟分布相对 train 系统性下移，train 标定的振幅分位点外推失效。10 重启 ×60 迭代单独重拟合确认 logL 收敛到 −0.7112（与 3 重启完全相同）——<b>不是局部最优，是分布漂移</b>。</li>
<li><b>FG top-1 仅 0.177</b>（nll 2.183 却仍排第 13）：挤仓年 FG 出现双峰漂移，holdout 实际类别分布 [0.003, 0.483, <b>0.000</b>, 0.395, 0.118]——class-2 整箱为空（train 分位点 0.1235/0.1629 之间无样本落入）。<b>nll 口径的比较仍然有效，top-1 在分布畸变下失真</b>——这是「用分位数分箱做外推」的固有弱点，报告如实并列两个数字。</li>
</ul></div>
<p class="mut">链的无效名单：L/FG/SA 上一阶链输给边际（过拟合转移结构），V/IF 与边际打平（差 &lt;0.01 bit）。IF 虽在链上无效，其 HMM 增益（+0.34）仍在——隐状态框架比离散链更稳健，因为它建模的是连续振幅发射而非离散状态的硬转移。</p>

<h2>二、极端行情专题：2020 与 2026</h2>

<h3>2.1 2026 春季复盘：板块级挤仓崩塌</h3>
<p>holdout（2026-01-05~06-12）不是普通的外推样本，而是一场真实的板块级极端行情。MA 与 EG 同在 04-07 见顶（3603 / 5814），04-08 板块级崩塌：MA 跳空 −1482bp、首根振幅 213 点、成交量 15.7 万手（平日的 10–30 倍）<b>而持仓量仅 −1%</b>——是挤仓崩塌（多头踩踏、流动性枯竭）而非基本面重定价；至 06-12 MA −15.8% / EG −19.1%。</p>
<div class="fig">{F.f0()}</div>
<p>数据审计确认这不是数据污染：2026 春季的会话间跳空为真实事件（MA 2026-04-08 跳空 −1604bp 为全历史最大，剔除全部 334/329 个极端事件会话后开盘 15 分钟振幅占比仅从 0.589/0.590 降到 0.585、|r| 从 9.96/9.16 降到 9.06/8.02bp，U 型结论稳健）；EG 全部 38 个会话内 open≠前收 事件都在 2020 年 21:01（幅度 ≤477bp，占 0.007%），与 2026 无关。</p>

<h3>2.2 七窗口指标对比</h3>
<table>
<tr><th>指标</th><th>2020 崩盘</th><th>2020 反弹</th><th>2021 牛</th><th>2025 关税</th><th>2026 挤仓</th><th>2024 平静</th><th>训练集</th></tr>''')
def row(name, key, fmt='{:.3f}', f=None):
    cells = []
    for w in WINS:
        v = EXT['windows']['MA'][w][key] if f is None else f(EXT['windows']['MA'][w])
        cells.append(fmt.format(v))
    return f'<tr><td class="l">{name}</td>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
H.append(row('MA 振幅均值（%）', 'mean_amp', '{:.4f}'))
H.append(row('MA |单根收益|（bp）', 'mean_absr_bp', '{:.2f}'))
H.append(row('MA 8 态熵 H', 'H_state'))
H.append(row('MA 转移自保持', 'trans_diag'))
H.append(row('MA 状态平均持续期', 'mean_run'))
H.append(row('MA clv→下根收益 IC', 'ic_clv_rc'))
H.append(row('MA 开盘15分钟|r|（bp）', 'absr_first15_bp', '{:.1f}'))
H.append(row('MA 高波态占比', 'hmm_occ', '{:.3f}', lambda v: v['hmm_occ'][2]))
H.append(row('MA 切换率/千根', 'hmm_switches_per_1000', '{:.0f}'))
H.append(f'''</table>
<div class="fig">{F.f2()}</div>
<div class="box"><b>三个跨窗口稳健发现：</b>
<ol class="tight">
<li><b>持续型行情 = 体制翻转；跳空型事件 ≠ 体制翻转</b>：四个持续型窗口（2020 崩盘/反弹、2021 牛、2026 挤仓）高波态占比 MA 71–95% / EG 68–87%（EG 2020 崩盘 32.7% 是例外：急跌后迅速反弹），平静年 MA 13.4% / EG 4.6%。但 <b>2025-04 关税崩盘只有 MA 22.6% / EG 8.8%</b>——一次 −652bp 的跳空（MA 04-07）之后是平静的月末，平均振幅 0.105% 仅为挤仓窗口（0.227%）的一半。<b>「出现极端 K 线」不改变状态分布，「进入极端体制」才改变</b>——这是本节最重要的区分。切换率佐证同一叙事但只对 MA 成立：MA 在三个持续型窗口 11–53 vs 平静年 371（低一个数量级），而 EG 无此模式（44–97 全区间），解读时不可跨品种套用。</li>
<li><b>clv 均值回归是崩盘属性、趋势中消失</b>：MA 的 clv→下一根收益 IC 在崩盘/平静窗口 −0.13~−0.25，在趋势窗口（2021 牛 −0.06、2026 挤仓 −0.06）<b>衰减一半以上</b>；EG 同样（−0.13/−0.08 → −0.05/−0.03）。2026 挤仓窗口 IC 接近零——单边趋势中「收在极值」不再预示回拉。</li>
<li><b>开盘尖峰随波动率同比放大</b>：SQUEEZE26 开盘 15 分钟 |r| 20.4bp vs 日内其余 ~10.6bp；平静年只有 6.4bp vs ~3bp。开盘集中度的<b>形状</b>在所有窗口稳定（首 15 分钟占全日 55–60%），变的是幅度。</li>
</ol></div>

<h3>2.3 共尾的双基准：压力放大 vs 结构共尾</h3>
<p>同一个共尾事实必须分两个口径报告（此前的深挖报告用窗口自身基准、本章用 train 标定基准，两者回答不同问题，不可混用）：</p>
<div class="fig">{F.f3()}</div>
<table>
<tr><th>窗口</th><th>联合概率</th><th>train 标定<br>压力放大</th><th>窗口自身<br>结构共尾</th><th>naive 基准<br>（失真警示）</th></tr>''')
for w in WINS:
    c = EXT['cotail'][w]
    H.append(f'<tr><td class="l">{WINLAB[w]}</td><td>{c["p_joint"]:.5f}</td><td><b>{c["mult_slot"]:.1f}×</b></td>'
             f'<td>{c["mult_win"]:.2f}×</td><td>{c["mult_naive"]:.1f}×</td></tr>')
H.append(f'''</table>
<div class="box info"><b>两个口径给出两个都真实的结论：</b>
<ul class="tight">
<li><b>压力放大（train 基准）</b>：五个极端窗口 25.6–57.4×——相对全历史常态，极端行情里共尾被放大 1–2 个数量级；平静年 0.31×（低于 1，即极端分钟比期望更<b>少</b>地成对出现）。</li>
<li><b>结构共尾（窗口自身基准）</b>：2.4–4.6× 在所有极端窗口稳定——一旦进入高波体制，共尾的结构比例并不随行情烈度变化；<b>变的是基准（波动率水平），不是共尾结构</b>。</li>
<li><b>naive 基准的陷阱</b>：2025-04 关税窗口 naive 基准给出 68.2× 的虚高（当月自身高频极端事件把基准压扁），slot 调整后回落到 2.16×。<b>共尾倍数对基准的选择比对数据更敏感</b>——这是本章最重要的方法学警示。</li>
</ul></div>

<h2>三、可交易性：1 分钟尺度的成本判决</h2>
<h3>3.1 设置</h3>
<p>把此前发现的 clv 均值回归（IC ≈ −0.20）按最诚实的方式做成策略：clv 收在极值格点（&gt;0.8 / &lt;0.2，train 标定）时反向持有，h ∈ {{1, 5, 15, 60}} 分钟；成本以名义本金 bp 表示——佣金 0.5/1/2‱ 每边 + 滑点 2.5/5/10bp 每边，参考成本取往返 ~12bp（每边 ~6bp）。仓位按目标波动率归一（成本压力测试权重归一到名义本金比例，避免 z-score 当仓位用的口径错误）。</p>
<h3>3.2 结果：毛边缘真实存在，但差成本一个数量级</h3>
<div class="fig">{F.f4()}</div>
<table>
<tr><th>品种</th><th>h=1 净收益<br>(bp/笔)</th><th>h=1 毛边缘</th><th>h=5 净收益</th><th>h=15 净收益</th><th>h=60 净收益</th><th>胜率</th><th>笔数(h=1)</th></tr>''')
for s in sorted(SYMS15, key=lambda k: -BT[k]['runs']['h1_train']['gross_bp']):
    r = {h: BT[s]['runs'][f'{h}_train'] for h in ['h1', 'h5', 'h15', 'h60']}
    H.append(f'<tr><td>{s}</td><td><b class="r">{r["h1"]["net_bp"]:+.2f}</b></td><td>{r["h1"]["gross_bp"]:+.2f}</td>'
             f'<td>{r["h5"]["net_bp"]:+.2f}</td><td>{r["h15"]["net_bp"]:+.2f}</td><td>{r["h60"]["net_bp"]:+.2f}</td>'
             f'<td>{r["h1"]["win"]*100:.1f}%</td><td>{r["h1"]["n"]:,}</td></tr>')
H.append(f'''</table>
<p>holdout 完全同构（如 MA h=1：毛 +1.57bp、净 −10.43bp）。调整阈值、时段过滤、持有期都不改变结论——毛边缘在 h=1 到 h=60 之间几乎不变（MA 1.96→2.14bp），说明这不是「持有期不够长」的问题。</p>
<h3>3.3 边缘的来源分解：一个微观结构签名，不是一个 alpha</h3>
<div class="box"><b>池化证据：</b>15 品种 × h=1 × train = 317.9 万笔，池化毛边缘 <b>+1.46bp/笔（t=329.7，n=3,179,075）</b>——统计上无可争议。但盈亏平衡往返成本就是 1.46bp = 每边 0.73bp，而现实每边 ~6bp，<b>差 8 倍</b>。分解后边缘的特征更像买卖价差的影子：
<ul class="tight">
<li><b>全部集中在 clv ∈ {{0, 1}} 极端格点</b>（收在当根最高/最低），中间格点贡献为零——「下一根回拉」在分钟尺度上主要是价格从极值点回归，而非方向性预测；</li>
<li><b>多空完全对称</b>（MA +1.95 / +1.97bp，FG +3.39 / +3.31bp）——没有方向偏斜，符合微观结构噪声特征；</li>
<li><b>IF 为零</b>——股指的 tick 结构（最小变动 0.2 点、无连续做市）不产生这个签名；</li>
<li><b>开盘与非开盘无差异</b>（1.99 vs 1.95bp）——与「开盘微观结构」解释矛盾，更接近普遍的价差回归。</li>
</ul>
毛边缘最大的 FG（3.35bp）/SA（2.78bp）/UR（2.42bp）也不够：即使按最乐观成本口径（往返 4bp）也仅剩 ~1bp，且这还未计入冲击成本与容量限制。</div>
<div class="box bad"><b>结论（判决）：</b>1 分钟 clv 均值回归在数学上真实（t=329.7）、在实盘上不可交易（差成本 8 倍）。它不是「参数没调好」——阈值、时段、持有期、品种四个维度全部扫过；它是<b>尺度问题</b>：1 分钟尺度的微观结构边缘 ≈ 半个价差，天然属于做市商而不属于趋势跟随者。后续若要做可交易信号，必须换到 15 分钟以上的尺度或换到截面（相对价值）口径——后者见第一章的板块共尾结构。</div>

<h2>四、Kronos 基础模型零样本基准</h2><h3>4.1 设置</h3>
<p>把清华 Kronos（Kronos-base，两级残差量化 tokenizer + GPT 式解码）直接用于与 HMM/链<b>完全同口径</b>的任务：下一根振幅五档、holdout 2026、log-loss + top-1。每个评估点取前 512 根上下文（OHLCV+成交额 6 特征、上下文内 z-score 归一），内联复制其 <code>auto_regressive_inference</code> 采样循环但<b>保留每个样本的 OHLC 而不求均值</b>（原版对多样本取均值会毁掉分布信息），每点 20 样本（T=1.0 / top_p=0.95）构建五档经验分布（加 0.5 平滑），均匀先验基线 2.322 bit。</p>
<h3>4.2 结果：大幅落后于轻量模型</h3>
<div class="fig">{F.f6()}</div>''')
for s in ['MA', 'EG']:
    if s in KRO:
        k = KRO[s]
        note = '（冒烟小样本，N=200 重跑进行中）' if k['n'] < 100 else ''
        H.append(f'<p><b>{s}</b>：Kronos nll <b>{k["nll"]:.3f}±{k["nll_se"]:.3f}</b>（n={k["n"]}{note}）、top-1 {k["top1"]:.3f}；'
                 f'对照 HMM {HMM[s]["hmm_nll"]:.3f} / top-1 {HMM[s]["hmm_top1"]:.3f}，一阶链 {CHN[s]["m1"]:.3f}，边际 {CHN[s]["marg"]:.3f}。</p>')
H.append(f'''<div class="box"><b>解读：</b>Kronos 的 nll（MA 4.05±0.17、EG 4.31±0.16，均 N=200）比均匀基线（2.32）还差约 1.8 倍，top-1 0.22–0.27 远低于 HMM 的 0.48–0.48，接近随机（0.20）。预测类别直方图严重失衡：EG 的 4,000 个预测点几乎全部坍缩到最低振幅档 [1,0,0,0,0]（真实分布 ≈ 均匀五档），MA 坍缩较轻 [89,14,32,24,41] 但系统性偏移——面对「下一分钟振幅」这种近噪声任务，基础模型输出的是过度平滑/错位的「平均行为」。
<ul class="tight">
<li><b>为什么落后</b>：两级残差量化 token 把价格离散成有限词表，自回归解码在 512 上下文里学到的是「典型的下一根长什么样」，而分钟振幅的可预测部分（HMM 抓住的 0.26–0.65 bit）是<b>条件分布的形状</b>而非典型值——需要用发射模型显式建模的部分恰好是采样均值会抹掉的部分。</li>
<li><b>边界（不扩大化结论）</b>：这是<b>零样本</b>、CPU、N=200 的基准；Kronos 的设计目标是长上下文价格形态（其论文场景是天/小时级序列），本任务（1 分钟振幅五档）落在其能力圈外。本结论只断言：<b>在该微结构任务上，通用基础模型不优于一个 3 状态 HMM</b>，不构成对 Kronos 在其他任务上能力的评价。</li>
</ul></div>

<h2>五、时间维度实验：把「何时交易」显式入模</h2>
<h3>5.1 动机与设计</h3>
<p>六位编码只描述「这一根长什么样」（实体方向/上影/下影），不含时间坐标。此前时间因素只作为<b>条件基准</b>隐性使用（共尾的 slot 调整、开盘集中度），从未作为<b>预测特征</b>正式入模。本节把它入模并量化：用交易日的 345 个 slot（会话内序号 + 会话起点偏移）作为显式条件，在阶梯模型上量测每一级增加 slot 带来的 holdout log-loss 改善（MA/EG/TA 三品种，train 标定 + EB 收缩，所有条件变量一律取 t−1，防止同根泄露）：</p>
<div class="fig">{F.f7()}</div>
<h3>5.2 结果：信息真实存在，但几乎全部集中在两个开盘 15 分钟</h3>
<table>
<tr><th>模型（幅度任务，holdout nll）</th><th>MA</th><th>EG</th><th>TA</th><th class="l">slot 的条件增量</th></tr>
<tr><td>边际</td><td>2.336</td><td>2.343</td><td>2.342</td><td class="l">—</td></tr>
<tr><td>+ slot</td><td>2.300</td><td>2.322</td><td>2.317</td><td class="l"><b>+0.021~0.036 bit</b></td></tr>
<tr><td>前状态链</td><td>2.282</td><td>2.280</td><td>2.280</td><td class="l">—</td></tr>
<tr><td>前状态链 × slot</td><td>2.272</td><td>2.275</td><td>2.286</td><td class="l">±0.01（TA 反而过拟合）</td></tr>
<tr><td>前波幅（t−1 振幅档）</td><td>1.965</td><td>1.873</td><td>2.006</td><td class="l">—</td></tr>
<tr><td>前波幅 × slot</td><td>2.018</td><td>1.963</td><td>2.047</td><td class="l"><b>−0.05（反而变差！收缩噪声）</b></td></tr>
<tr><td>链 × 前波幅</td><td>1.949</td><td>1.867</td><td>2.000</td><td class="l">—</td></tr>
<tr><td><b>全件（链 × 前波幅 × slot）</b></td><td><b>1.945</b></td><td><b>1.864</b></td><td><b>1.995</b></td><td class="l"><b>+0.003~0.005 bit</b></td></tr>
</table>
<ul class="tight">
<li><b>slot 单条件增益的时间分布</b>：09:00–09:14 开盘 +0.49~+0.61 bit、21:00–21:14 夜盘开盘 +0.55~+0.76 bit（3 品种一致）；09:15–10:15 还有 +0.06~0.10；<b>其余六个时段全部 ≤0</b>（−0.01~−0.13，纯收缩噪声）。开盘后的 bars 振幅分布与其余时间完全不同（均值 16.4bp vs 盘中 11.3–11.8bp），slot 在那里是强信息，别处不是。</li>
<li><b>但增量是「共线」的</b>：一旦模型里已经有前一根振幅（vol1），slot 的条件增量从 +0.02~0.04 缩到 +0.003~0.005 bit——开盘的高波幅大部分已被「前波幅 + 链」捕获。<b>甚至前波幅×slot 比前波幅更差（−0.05 bit）</b>：345 个 slot × 5 档的条件分布在盘中 cell 里只剩收缩先验，粗粒度条件反而引入方差。</li>
<li><b>方向任务 slot 增益 ≈ 0</b>：MA holdout 0.9793（边际）→ 0.9792（+slot）；在 clv 五档之上再加 slot 也只有 +0.001。方向信息的载体是 clv（+0.012 bit），不是时间。</li>
<li><b>链本身是时间非齐次的</b>：8×8 转移矩阵在开盘块偏离全池最多（TV 0.105/0.105），盘中只有 0.015–0.035——「开盘时编码动力学不同」是真的，但这个不同已经被链×slot 吸收后也只剩零点零几 bit。</li>
<li><b>clv 均值回归的时段定位（train）</b>：夜盘收盘最强（23:00 块 IC −0.252）、夜盘中段 −0.232、日盘盘中 −0.20；<b>开盘最弱</b>（09:00 块 −0.138、21:00 块 −0.117）——开盘是信息到达主导（跳空后回归弱），收盘是仓位回补主导（回归强）。与第三章「开盘/非开盘毛边缘无差异」并列看：MR 的强度随时段变化，但把它变成策略仍过不了成本。</li>
</ul>
<div class="box"><b>工程结论：</b>若要给任何「下一根振幅预测器」加时间维度，<b>不要喂 345 个 slot——只喂两个开盘哑变量</b>（日盘开盘前 15 分钟、夜盘开盘前 15 分钟）就拿到了时间维度几乎全部信息，还避开了盘中 345 路收缩噪声。这与其说是发现「时间有价值」，不如说是精确定位了时间价值的边界：<b>时间维度的信息 = 开盘信息的别名</b>。</div>

<h2>六、口径与局限声明</h2>
<div class="box warn"><b>阅读本章数字前必须知道的六件事：</b>
<ol class="tight">
<li><b>holdout 只有 5.3 个月</b>（2026-01-05~06-12），且是极端体制样本（2026 挤仓）。所有 holdout 数字都带「小样本 + 分布漂移」双重折扣，FG/UR 的畸变已定量证明。</li>
<li><b>共尾有两个基准</b>：train 标定（压力放大 25.6–57.4×）与窗口自身（结构共尾 2.4–4.6×）。前者回答「比平时糟多少」，后者回答「结构上共尾多少」。单引一个数字而不说基准 = 错误引用。深挖报告与本章表面上的「矛盾」（3.55× vs 25.9×）全部来自基准不同。</li>
<li><b>振幅五档分位点在 holdout 上外推失效</b>（FG class-2 空箱、UR 夜盘取消）——分位数分箱是 train 标定的，分布漂移下 top-1 失真；nll 口径受影响小得多但仍非完全免疫。</li>
<li><b>新品种历史短</b>：PR 仅两年、PX 不足三年、PF 一年余。它们的转移矩阵/共尾估计误差大于老品种，跨品种排名（尤其涉及 PR/PX 的）应视为方向性而非精确。</li>
<li><b>IF 不是商品</b>：tick 结构、做市制度、无夜盘全部不同，其零 MR 边缘与链无效是口径差异的正常结果，不构成「化工规律失效」的证据。</li>
<li><b>Kronos 数字的误差棒</b>：N=200 时 nll 标准误 ~±0.16，与 HMM 的差距（2.4 bit）远超误差棒；但 CPU 推理、20 样本/点的设置意味着绝对水平可能随设置漂移，<b>结论只基于同设置下的相对比较</b>。</li>
</ol></div>

<h2>附：复现指南</h2>
<table>
<tr><th class="l">步骤</th><th class="l">脚本</th><th class="l">产物</th></tr>
<tr><td class="l">盲区扫描 + 数据完整性</td><td class="l"><code>step32_gap_scan.py</code> / <code>step32b_contamination.py</code></td><td class="l">results_gap.json / 污染复核</td></tr>
<tr><td class="l">13 品种提取</td><td class="l"><code>step33_extract_sector.py</code></td><td class="l">15 品种三段 1m CSV</td></tr>
<tr><td class="l">极端七窗口</td><td class="l"><code>step34_extreme.py</code></td><td class="l">results_extreme.json</td></tr>
<tr><td class="l">板块共尾/同质性/相关</td><td class="l"><code>step35_sector.py</code> / <code>step35b_sector_chain.py</code></td><td class="l">results_sector.json / _chain.json</td></tr>
<tr><td class="l">板块 HMM + 例外诊断</td><td class="l"><code>step36_hmm_sector.py</code> / <code>step36b_ur_refit.py</code> / <code>step36c_fg_ur_diag.py</code></td><td class="l">results_sector_hmm.json / _ur_refit.json</td></tr>
<tr><td class="l">可交易性回测</td><td class="l"><code>step37_backtest.py</code> / <code>step37b_backtest_diag.py</code></td><td class="l">results_backtest.json / _diag.json</td></tr>
<tr><td class="l">Kronos 基准</td><td class="l"><code>step38_kronos_bench.py</code></td><td class="l">results_kronos.json</td></tr>
<tr><td class="l">时间维度入模</td><td class="l"><code>step40_time_dim.py</code></td><td class="l">results_time_dim.json</td></tr>
<tr><td class="l">本章图表 + 报告</td><td class="l"><code>step39_figs.py</code> / <code>step39_ch2_report.py</code></td><td class="l">ch2_report.html</td></tr>
</table>
<p class="mut">编码定义：实体方向/上影/下影各 2 位 → 8 态八卦；clv=(C−L)/(H−L)；振幅=(H−L)/O；slot=会话内序号+会话起点偏移（09:00=0 / 10:00=75 / 13:00=135 / 21:00=225）。共尾=两品种同一分钟双双突破各自 train 99 分位振幅；基准一律用分钟桶 slot 边际（EB 收缩 α=10）。</p>
</div></body></html>''')

open('ch2_report.html', 'w', encoding='utf-8').write(''.join(H))
print('ch2_report.html written,', len(''.join(H)), 'chars')
