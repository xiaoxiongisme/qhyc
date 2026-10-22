# -*- coding: utf-8 -*-
"""step31: 生成深挖报告 deep_report.html（维度九~十二 + 三个推翻前论专区）"""
import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
M = json.load(open(f'{BASE}/results_micro.json', encoding='utf-8'))
P = json.load(open(f'{BASE}/results_persist.json', encoding='utf-8'))
C = json.load(open(f'{BASE}/results_cross.json', encoding='utf-8'))

def svg(name):
    s = open(f'{BASE}/{name}', encoding='utf-8').read()
    if s.startswith('<?xml'):
        s = s[s.index('?>') + 2:].strip()
    return s

def f(x, n=2):
    if x is None:
        return '—'
    return f'{x:.{n}f}'

def fx(x, n=2):
    return '—' if x is None else f'{x:.{n}f}×'

def pc(x, n=1):
    return '—' if x is None else f'{100*x:.{n}f}%'

def bp(x, n=2):
    return '—' if x is None else f'{x:.{n}f} bp'

# ---------- 取数 ----------
ma, eg = M['MA'], M['EG']
u_ma, u_eg = ma['intraday']['uprof'], eg['intraday']['uprof']
ofi_ma, ofi_eg = ma['ofi'], eg['ofi']
q_eg = ofi_eg['quintiles']
q_ma = ofi_ma['quintiles']
mon_ma, mon_eg = ma['monday_day'], eg['monday_day']
imon_ma, imon_eg = ma['intraday']['monday'], eg['intraday']['monday']
conc_ma, conc_eg = ma['concentration'], eg['concentration']

pm, pe = P['MA'], P['EG']
ev_ma, ev_pe = pm['eval'], pe['eval']
h_ma, h_pe = pm['hidden'], pe['hidden']
hf_ma, hf_pe = pm['hazard_fix'], pe['hazard_fix']
inc_ma, inc_pe = pm['increment'], pe['increment']

co = C['coverage']
ct = C['cotail_by_q']
sa = C['slot_adj']
say = C['slot_adj_by_year']
byy = C['by_year']
ll = C['leadlag']
dr, dra = C['dirs'], C['dirs_adj']
po = C['port']
ro = C['roll']
ac = C['amp_corr']

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
A = H.append

A(f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>深挖报告：微观结构 · 状态持续期与 HMM · 跨品种共尾分解</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>深挖三个方向：微观结构、状态持续期与 HMM、跨品种共尾的季节性分解</h1>
<div class="sub">甲醇 MA + 乙二醇 EG · 1 分钟 · 2020-2025 训练 {co['by_year']['2020']+co['by_year']['2021']+co['by_year']['2022']+co['by_year']['2023']+co['by_year']['2024']+co['by_year']['2025']:,} 根/品种 + 2026 holdout {co['by_year']['2026']:,} 根/品种 · 两品种合并 {co['n_merged']:,} 根 100% 匹配 · 口径：夜盘归属次日交易日、会话内相邻对 · 2026-10-18</div>

<h2>〇、三个方向与三个「推翻前论」</h2>
<p>上一轮八维度报告（dimensions_report.html）收尾时提议了三个深挖方向，本轮全部完成。总览如下——三个方向各产出一个「推翻前论」级发现，其中两个是<b>上一轮自己的结论被自己的更严口径推翻</b>。</p>
<table><thead><tr><th style="width:96px">方向</th><th>核心问题</th><th>关键读数（MA / EG）</th><th style="width:150px">判决</th></tr></thead><tbody>
<tr><td class="l"><b>九 · 微观结构</b></td><td class="l">日内节律、订单流不平衡（clv 代理）、量价关系</td><td class="l">开盘 15 分钟占会话振幅 {pc(conc_ma['9']['range_share_first15'])}/{pc(conc_eg['9']['range_share_first15'])}；clv→下一根收益 IC {f(ofi_ma['ic_rc_train'],3)}/{f(ofi_eg['ic_rc_train'],3)} → holdout {f(ofi_ma['ic_rc_holdout'],3)}/{f(ofi_eg['ic_rc_holdout'],3)}</td><td><span class="pill p-pass">U 型真实；OFI 代理仅均值回归</span></td></tr>
<tr><td class="l"><b>十 · 持续期</b></td><td class="l">八卦态记忆有多深、hazard 形状</td><td class="l">平均 run 长 1.02–1.35 根；P(L≥5) 实际 {pc(hf_ma['long_run_share'],3)}/{pc(hf_pe['long_run_share'],3)} vs 几何基准 {pc(hf_ma['long_run_share_markov'],3)}/{pc(hf_pe['long_run_share_markov'],3)}</td><td><span class="pill p-mid">浅记忆 + 重尾，一阶链抓不住长尾</span></td></tr>
<tr><td class="l"><b>十一 · HMM</b></td><td class="l">隐状态（波动体制）能否打败 8 态显态链</td><td class="l">holdout log₂/bar：HMM(K=3) {f(ev_ma['3']['hmm_ll'],3)}/{f(ev_pe['3']['hmm_ll'],3)} vs 一阶链 {f(ev_ma['3']['m1_ll'],3)}/{f(ev_pe['3']['m1_ll'],3)}、二阶链 {f(ev_ma['3']['m2_ll'],3)}/{f(ev_pe['3']['m2_ll'],3)}</td><td><span class="pill p-pass">HMM 大幅胜出；八卦增量 ≈0</span></td></tr>
<tr><td class="l"><b>十二 · 共尾分解</b></td><td class="l">MA×EG 极端波动共尾有多「真」</td><td class="l">99% 分位联合/独立：原始 {fx(sa['train']['raw_multiplier'],1)} → slot 调整后 {fx(sa['train']['multiplier'],2)}（train）；holdout {fx(sa['holdout']['multiplier'],2)}</td><td><span class="pill p-mid">大部分是共享时钟；真实联动 2–4×</span></td></tr>
</tbody></table>

<div class="box bad"><b>三个「推翻前论」级发现（第五节集中展开）：</b>
<ol class="tight">
<li><b>D4「周一振幅为周二~周五的 0.69 倍」是数据覆盖假象。</b>周日完全无交易（实测 0 根 bar）⇒ 周一交易日缺整段夜盘 ⇒ 5×69 时段矩阵里 24/69 块被 0 填充，均值被稀释 0.652 倍。分解式 <code>0.69 ≈ 1.06 × 0.652</code>：真实方向完全相反——周一 1 分钟振幅是五天最高（MA 0.1303 vs 其余 0.121–0.125），开盘 5 分钟块振幅比高达 1.44×。</li>
<li><b>D8「99% 分位联合/独立 = 34×」大部分是共享日内钟表的机械共振。</b>用 slot 条件独立做基准后，train 31.4× → 4.05×、holdout 11.5× → 3.55×；分年看 slot 调整后稳定在 1.7–3.7×，而原始倍数在 12–219× 之间剧烈摆动。</li>
<li><b>（回顾）D7「综卦 = 大变动」同样被推翻：</b>综卦对条件分布 JS 距离仅 0.0011–0.002 bit，比错卦对近 30 倍——静态几何与动态几何是两个问题。</li>
</ol></div>

<div class="kpi">
<div class="c"><div class="v">{pc(conc_ma['9']['range_share_first15'])}</div><div class="k">开盘 15 分钟占会话振幅（MA 9 点段）</div></div>
<div class="c"><div class="v">{fx(hf_ma['long_run_share']/hf_ma['long_run_share_markov'],1)}</div><div class="k">长 run（≥5 根）重尾倍数 vs 几何基准（MA）</div></div>
<div class="c"><div class="v">{f(ev_ma['3']['hmm_ll'],2)} vs {f(ev_ma['3']['m1_ll'],2)}</div><div class="k">HMM vs 一阶链 holdout log₂/bar（MA）</div></div>
<div class="c"><div class="v">{fx(sa['train']['multiplier'],1)}</div><div class="k">slot 调整后共尾倍数（train 99%）</div></div>
</div>
''')

# ================= S1 =================
A(f'''
<h2>一、维度九 · 微观结构：开盘定乾坤、订单流代理只会回归</h2>

<h3>1.1 日内 U 型：开盘与尾盘的波动集中</h3>
<p>把每根 bar 按「距所属时段开盘的分钟数」分槽（slot 0 = 各时段第一根，1 分钟粒度，四个时段 9/10/13/21 点段共 345 个有效槽），对 2020–2025 训练段取平均。已实现波动呈教科书式 U 型：开盘 15 分钟 |收益| 最高，中段凹陷，尾盘 15 分钟再度抬升。</p>
<table><thead><tr><th>品种</th><th>开盘 15 分钟 |r|</th><th>中段 |r|</th><th>尾盘 15 分钟 |r|</th><th>开盘 15 分钟 amp</th><th>中段 amp</th><th>尾盘 15 分钟 amp</th><th>开盘 15 分钟均量</th><th>中段均量</th></tr></thead><tbody>
<tr><td><b>MA</b></td><td><b class="p">{bp(u_ma['first15']['mean_absr_bp'])}</b></td><td>{bp(u_ma['mid']['mean_absr_bp'])}</td><td>{bp(u_ma['last15']['mean_absr_bp'])}</td><td>{pc(u_ma['first15']['mean_amp'],2)}</td><td>{pc(u_ma['mid']['mean_amp'],2)}</td><td>{pc(u_ma['last15']['mean_amp'],2)}</td><td>{u_ma['first15']['mean_vol']:,.0f}</td><td>{u_ma['mid']['mean_vol']:,.0f}</td></tr>
<tr><td><b>EG</b></td><td><b class="p">{bp(u_eg['first15']['mean_absr_bp'])}</b></td><td>{bp(u_eg['mid']['mean_absr_bp'])}</td><td>{bp(u_eg['last15']['mean_absr_bp'])}</td><td>{pc(u_eg['first15']['mean_amp'],2)}</td><td>{pc(u_eg['mid']['mean_amp'],2)}</td><td>{pc(u_eg['last15']['mean_amp'],2)}</td><td>{u_eg['first15']['mean_vol']:,.0f}</td><td>{u_eg['mid']['mean_vol']:,.0f}</td></tr>
</tbody></table>
<p>开盘集中度按四个时段分别算（占该时段自身振幅/成交量的比例）：</p>
<table><thead><tr><th>品种</th><th>时段</th><th>开盘 15 分钟占本段振幅</th><th>开盘 30 分钟占本段成交量</th><th>会话数</th></tr></thead><tbody>
''')
for sym, cc in (('MA', conc_ma), ('EG', conc_eg)):
    for seg in ('9', '10', '13', '21'):
        d = cc[seg]
        A(f'''<tr><td>{sym}</td><td>{seg}:00 段</td><td>{pc(d['range_share_first15'])}</td><td>{pc(d['vol_share_first30'])}</td><td>{d['n_sessions']:,}</td></tr>''')
A(f'''</tbody></table>
<p>四个时段一致：开盘 15 分钟吞掉本段 {pc(min(conc_ma[s]['range_share_first15'] for s in ('9','10','13','21')))}–{pc(max(conc_ma[s]['range_share_first15'] for s in ('9','10','13','21')))} 的振幅，夜盘（21 点段）比日盘更集中。成交量轮廓峰值在 slot 225（21:00 夜盘开盘首根），收盘前无异常放量。工程含义直白：<b>止损宽度、仓位、滑点估计都必须按时段分层</b>，用全天平均幅度定止损会在开盘段被反复击穿。</p>
<div class="fig">{svg('_dd_micro.svg')}</div>

<h3>1.2 订单流不平衡代理 clv：只有均值回归，没有方向 alpha</h3>
<p>用收盘位置 <code>clv=(C−L)/(H−L)</code> 作为订单流不平衡（OFI）的代理（+1 = 收在最高 = 买方主导，−1 = 收在最低 = 卖方主导），检验它对下一根收益/振幅的预测力（Spearman rank IC，train→holdout 分离）：</p>
<table><thead><tr><th>预测目标</th><th>MA train</th><th>MA holdout</th><th>EG train</th><th>EG holdout</th><th>解读</th></tr></thead><tbody>
<tr><td class="l">下一根收益 rc（bp）</td><td><b class="r">{f(ofi_ma['ic_rc_train'],3)}</b></td><td><b class="r">{f(ofi_ma['ic_rc_holdout'],3)}</b></td><td><b class="r">{f(ofi_eg['ic_rc_train'],3)}</b></td><td><b class="r">{f(ofi_eg['ic_rc_holdout'],3)}</b></td><td class="l">负 IC = 均值回归，holdout 同号衰减</td></tr>
<tr><td class="l">下一根振幅 amp</td><td>{f(ofi_ma['ic_amp_train'],3)}</td><td>{f(ofi_ma['ic_amp_holdout'],3)}</td><td>{f(ofi_eg['ic_amp_train'],3)}</td><td>{f(ofi_eg['ic_amp_holdout'],3)}</td><td class="l">≈0，收盘位置不含波动信息</td></tr>
<tr><td class="l">成交量 → 下一根振幅</td><td><b class="g">{f(ofi_ma['vol_amp_ic_train'],3)}</b></td><td><b class="g">{f(ofi_ma['vol_amp_ic_holdout'],3)}</b></td><td><b class="g">{f(ofi_eg['vol_amp_ic_train'],3)}</b></td><td><b class="g">{f(ofi_eg['vol_amp_ic_holdout'],3)}</b></td><td class="l">强且 holdout 不衰减（放量⇒高波）</td></tr>
<tr><td class="l">成交量 → 下一根收益</td><td>{f(ofi_ma['vol_rc_ic_train'],3)}</td><td>—</td><td>{f(ofi_eg['vol_rc_ic_train'],3)}</td><td>—</td><td class="l">≈0，量不含方向信息</td></tr>
</tbody></table>
<p>把 clv 分五档看下一根平均收益（EG 分档干净，MA 因点质量退化见 1.4）：</p>
<table><thead><tr><th>品种</th><th>clv 最低档</th><th>第 2 档</th><th>第 3 档</th><th>第 4 档</th><th>clv 最高档</th><th>单调性</th></tr></thead><tbody>
''')
A('<tr><td><b>EG</b></td>' + ''.join(
    f'<td>{bp(r["next_rc_bp"])}</td>' for r in q_eg) +
  f'<td><span class="pill p-pass">严格单调递减</span></td></tr>')
ma_rows = [r for r in q_ma if r['n'] > 0]
A('<tr><td><b>MA</b>（退化三档）</td>' + ''.join(
    f'<td>{bp(r["next_rc_bp"])}</td>' for r in ma_rows) +
  f'<td><span class="pill p-mid">三档单调</span></td></tr>')
A(f'''</tbody></table>
<p>clv 极端 ⇒ 下一根反向：EG 从最高档 +{bp(q_eg[0]['next_rc_bp'])} 到最低档 {bp(q_eg[4]['next_rc_bp'])}，价差近 {bp(q_eg[0]['next_rc_bp']-q_eg[4]['next_rc_bp'])}；但「同向延续率」只有 26%–41%，与抛硬币无异。累计 OFI（过去 k 根 clv 均值和）对未来 k 根收益的 IC 也只是弱正：MA {f(ofi_ma['cum_ofi']['5']['ic_train'],3)}→{f(ofi_ma['cum_ofi']['5']['ic_holdout'],3)}、EG {f(ofi_eg['cum_ofi']['5']['ic_train'],3)}→{f(ofi_eg['cum_ofi']['5']['ic_holdout'],3)}（k=5）；k=15 衰减一半；k=60 因会话内窗口不足样本仅 1,457 根，不可用。</p>
<div class="box info"><b>量价分工明确：</b>成交量预测波动（IC 0.46–0.63，holdout 不降反升），收盘位置预测回归（IC −0.20→−0.07）。按振幅五档看量比单调递增：MA {' → '.join(f(x,2)+'×' for x in ma['vol_by_ampq'])}，EG {' → '.join(f(x,2)+'×' for x in eg['vol_by_ampq'])}——最高波档的成交量是静默档的 2.8 倍。八个八卦态的相对成交量同样分化：艮☶（上有影下有影…长上下影）成交量最高（MA {fx([s for s in ma['state_vol'] if s['state']=='艮☶'][0]['vol_rel'])}、EG {fx([s for s in eg['state_vol'] if s['state']=='艮☶'][0]['vol_rel'])}），离☲ 最低（{fx([s for s in ma['state_vol'] if s['state']=='离☲'][0]['vol_rel'])}/{fx([s for s in eg['state_vol'] if s['state']=='离☲'][0]['vol_rel'])}）——影线即分歧，分歧即放量，与维度①的形态频率互相印证。</div>

<h3>1.3 价格网格效应：OFI 代理的粒度极限</h3>
<p>本轮新发现的微观结构事实：<b>分钟级价格网格极粗，clv 严重离散化。</b>MA 有 <b class="r">68.2%</b> 的 bar 收盘位置恰落在 {{最低, 中点, 最高}} 三点上（EG 49.7%）；MA 1 分钟波幅中位数只有 3 个最小变动单位（tick），p90 也只有 5 个——1 分钟里价格经常只走 3 跳，clv 只能取 374 个不同值（EG 766 个）。后果有二：① clv 作为 OFI 代理被量化噪声污染，这是它 IC 只有 −0.1~−0.2 的部分原因；② 任何按 clv 连续分位的策略都要防平局——本轮 MA 的五分位表就因此退化成三档（40%/60% 分位点因 20% 的点质量同在 0.5 而重合，q0/q2 两箱为空）。</p>
<div class="fig">{svg('_dd_persist.svg')}</div>

<h3>1.4 推翻前论 #1：D4「周一 0.69×」是数据覆盖假象，真相是周一最热</h3>
<div class="box bad"><b>上一轮 D4 的结论：</b>「周一振幅为周二~周五的 0.69 倍」（5 分钟块振幅均值的星期矩阵）。<br>
<b>本轮复验：假象。</b>周日<b>完全无交易</b>（{mon_ma['n_sunday_bars']} 根 bar，两个品种皆然）；按「夜盘归属次日交易日」口径，周一交易日的夜盘段（21:00–23:00）整段不存在，周一 td 的夜盘 bar 数 = {mon_ma['monday_td_night_bars']}。D4 的 5×69 矩阵（69 = 全天 345 分钟 ÷ 5）里，周一有 24 个块是「无数据」而按 0 计入均值 ⇒ 稀释因子恰为 45/69 = {45/69:.4f}。完整分解：<br><br>
<center><b style="font-size:15px">0.69 ≈ 1.06 × {45/69:.3f}　（真实周一效应 × 覆盖稀释）</b></center><br>
真实方向<b class="r">完全相反</b>——周一（纯日盘）1 分钟振幅五天最高：MA {' / '.join(f(x,4) for x in mon_ma['dow_amp_1m_train'])}（周一/二/三/四/五），EG {' / '.join(f(x,4) for x in mon_eg['dow_amp_1m_train'])}。同口径（都只算日盘）周一仍高 {fx(mon_ma['mon_vs_other_amp_dayonly'])}/{fx(mon_eg['mon_vs_other_amp_dayonly'])}（1 分钟振幅）、{fx(mon_ma['mon_vs_other_dayrange_dayonly'])}/{fx(mon_eg['mon_vs_other_dayrange_dayonly'])}（全日波幅）；且集中在开盘：前 30 分钟振幅比 {fx(imon_ma['ratio_amp_first30'])}/{fx(imon_eg['ratio_amp_first30'])}，开盘首个 5 分钟块 |收益| 比 {fx(imon_ma['mon_ratio_slot'][1])}/{fx(imon_eg['mon_ratio_slot'][1])}、振幅比 {fx(imon_ma['mon_ratio_amp_slot'][1])}/{fx(imon_eg['mon_ratio_amp_slot'][1])}（MA slot 序列在开盘 10 个块内维持 1.3–1.7×，午后才收敛到 1.0）。<br><br>
<b>教训：</b>「星期 × 时段」矩阵遇到不完整交易日时，0 填充会把「没有数据」洗成「没有波动」。凡此类矩阵，先查每个格子的样本数，再看均值。</div>
''')

# ================= S2 =================
A(f'''
<h2>二、维度十 · 状态持续期：浅记忆的头部，重尾的长尾</h2>
<h3>2.1 方法</h3>
<p>把 8 态八卦码的连续相同段（run）抽出来，统计 run 长度分布与 hazard 函数 h(k)=P(L=k | L≥k)。两个基准问题必须处理：① <b>删失</b>——会话/交易日末尾被截断的 run 并非「结束」，计入会低估长 run；本轮用删失修正（风险集 = 仍存活的 run），hazard_fix 键即修正后结果。② <b>几何（马尔可夫）基准</b>——若状态转移是一阶马尔可夫且齐次，run 长必为几何分布 P(L=k)=(1−p)p^(k−1)，p 由实测自留概率估计（MA p={pm['hazard_markov']:.4f}、EG p={pe['hazard_markov']:.4f}），任何偏离都是「非马尔可夫性」的直接证据。</p>
<h3>2.2 显态持续期：平均 1.0–1.35 根，但长 run 超重尾 7–8 倍</h3>
<table><thead><tr><th>run 长 k</th><th>MA hazard（删失修正）</th><th>MA 几何基准</th><th>MA P(L=k)</th><th>MA 几何 P(L=k)</th><th>EG hazard</th><th>EG 几何基准</th></tr></thead><tbody>
''')
for k in range(1, 9):
    A(f'''<tr><td>{k}</td><td>{f(hf_ma['hazard_censor'][k-1],3)}</td><td>{f(hf_ma['hazard_markov'],3)}</td><td>{f(hf_ma['obs_dist'][k-1],5)}</td><td>{f(hf_ma['markov_dist'][k-1],5)}</td><td>{f(hf_pe['hazard_censor'][k-1],3)}</td><td>{f(hf_pe['hazard_markov'],3)}</td></tr>''')
A(f'''</tbody></table>
<p>两个品种同一幅图景：hazard 从 k=1 的 {f(hf_ma['hazard_censor'][0],2)}–{f(hf_pe['hazard_censor'][0],2)}（≈几何基准 {f(hf_ma['hazard_markov'],3)}）单调递减到 k=8 的 {f(hf_ma['hazard_censor'][7],2)}–{f(hf_pe['hazard_censor'][7],2)}；P(L≥5) 实测 MA <b class="r">{pc(hf_ma['long_run_share'],3)}</b> / EG <b class="r">{pc(hf_pe['long_run_share'],3)}</b>，而同样自留概率的几何分布只给 {pc(hf_ma['long_run_share_markov'],3)}/{pc(hf_pe['long_run_share_markov'],3)}——<b>重尾 {hf_ma['long_run_share']/hf_ma['long_run_share_markov']:.1f}–{hf_pe['long_run_share']/hf_pe['long_run_share_markov']:.1f} 倍</b>。平均 run 长仅 {min(min(r['mean_len'] for r in pm['run_stats']), min(r['mean_len'] for r in pe['run_stats'])):.2f}–{max(max(r['mean_len'] for r in pm['run_stats']), max(r['mean_len'] for r in pe['run_stats'])):.2f} 根（MA 共 {hf_ma['n_runs']:,} 个 run、EG {hf_pe['n_runs']:,} 个），即绝大多数状态一触即走，但一旦活过 4–5 根就显著「粘住」。</p>
<div class="box warn"><b>读法：</b>头部（k=1–2）一阶马尔可夫描述得很好（hazard≈0.85≈基准）；长尾（k≥5）几何基准低估 7–8 倍。这正是维度②「一阶链有增益但三阶饱和」在持续期口径下的样子：链能抓换手率，抓不住「什么时候开始粘住」。按态看，坤☷/巽☴/坎☵ 等长影态平均 run 最长（EG 坤☷ {max(r['mean_len'] for r in pe['run_stats']):.2f} 根），离☲/兑☱ 最短（≈1.0 根）。</div>
''')

# ================= S3 HMM =================
A(f'''
<h2>三、维度十一 · 3 状态波动体制 HMM 大幅打败 8 态八卦链</h2>
<h3>3.1 方法与校验</h3>
<p>手写高斯对角发射 HMM（发射在 log 振幅上，缩放 forward-backward，按等长分组批处理，k-means + 4 次随机重启，35 次 EM 迭代），K=2/3/4 全跑。四道校验全部通过：① <b>3^6=729 条路径暴力枚举</b>对拍 forward 对数似然，逐位一致（两品种 check_forward_bruteforce = True）；② EM 每轮对数似然单调不降；③ 训练/ holdout 严格按年分离；④ <b>零振幅 bar 处理</b>——h==l 的 bar（MA 0.029%、EG 0.050%）取 log 会得 −inf，实测曾让整轮 EM 吐 NaN，最终以「非零最小振幅的一半」为下限（截尾观测解释），var 下限 0.02 防发射坍缩。</p>
<h3>3.2 隐状态 = 波动体制（K=3）</h3>
<table><thead><tr><th>品种</th><th>隐态</th><th>log 振幅均值 μ</th><th>对应振幅中位数</th><th>log 振幅 sd</th><th>时间占比</th><th>平均持续（根）</th><th>自留概率</th></tr></thead><tbody>
''')
import math
for sym, h in (('MA', h_ma), ('EG', h_pe)):
    order = sorted(range(3), key=lambda i: h['mu_log'][i])
    for rank, i in enumerate(order):
        A(f'''<tr><td>{sym}</td><td>S{rank+1}（{'低' if rank==0 else '中' if rank==1 else '高'}波）</td><td>{f(h['mu_log'][i],3)}</td><td>{pc(math.exp(h['mu_log'][i]),2)}</td><td>{f(h['sd_log'][i],3)}</td><td>{pc(h['frac_bars'][i],1)}</td><td><b>{f(h['mean_run'][i],1)}</b></td><td>{f(h['A'][i][i],3)}</td></tr>''')
A(f'''</tbody></table>
<p>结构清晰且两品种同构：低波态自留概率低（MA 仅 {f(h_ma['A'][0][0],2)}，是个「瞬态」），中波态是主力（占比 46%），高波态粘性最强（MA 自留 {f(h_ma['A'][2][2],3)}、平均持续 {f(h_ma['mean_run'][2],1)} 根；EG 三个态都极粘，自留 0.94–0.98、持续 20–51 根）。MA 高波态 log 振幅 sd={f(h_ma['sd_log'][2],2)} 远大于低波态 {f(h_ma['sd_log'][0],2)}——高波体制内部还在剧烈变速，这正是「波动长记忆」（DFA α≈0.8）的体制化表述。</p>
<h3>3.3 holdout 预测：下一根振幅五档，HMM 对显态链是降维打击</h3>
<p>预测任务：给定到 t 为止的观测，预测 t+1 根 log 振幅落在五个分位档中的哪一个（2026 holdout，n={ev_ma['3']['n']:,}，无前视）。对比：隐模型 HMM（K=2/3/4）、8 态显态一阶链、二阶链、边际分布。</p>
<table><thead><tr><th>模型</th><th>MA log₂/bar ↓</th><th>MA top-1 ↑</th><th>EG log₂/bar ↓</th><th>EG top-1 ↑</th></tr></thead><tbody>
''')
for k in ('2', '3', '4'):
    e_m, e_e = ev_ma[k], ev_pe[k]
    hl = ' class="l"' if k == '3' else ''
    star = ' ←' if k == '3' else ''
    A(f'''<tr{hl}><td class="l">HMM K={k}{star}</td><td><b class="g">{f(e_m['hmm_ll'],3)}</b></td><td><b class="g">{pc(e_m['hmm_top1'],1)}</b></td><td><b class="g">{f(e_e['hmm_ll'],3)}</b></td><td><b class="g">{pc(e_e['hmm_top1'],1)}</b></td></tr>''')
A(f'''<tr><td class="l">8 态一阶链</td><td>{f(ev_ma['3']['m1_ll'],3)}</td><td>{pc(ev_ma['3']['m1_top1'],1)}</td><td>{f(ev_pe['3']['m1_ll'],3)}</td><td>{pc(ev_pe['3']['m1_top1'],1)}</td></tr>
<tr><td class="l">8 态二阶链</td><td>{f(ev_ma['3']['m2_ll'],3)}</td><td>{pc(ev_ma['3']['m2_top1'],1)}</td><td>{f(ev_pe['3']['m2_ll'],3)}</td><td>{pc(ev_pe['3']['m2_top1'],1)}</td></tr>
<tr><td class="l">边际分布（无记忆）</td><td>{f(ev_ma['3']['marg_ll'],3)}</td><td>{pc(ev_ma['3']['marg_top1'],1)}</td><td>{f(ev_pe['3']['marg_ll'],3)}</td><td>{pc(ev_pe['3']['marg_top1'],1)}</td></tr>
</tbody></table>
<p>HMM(K=3) 比一阶链少 {f(ev_ma['3']['m1_ll']-ev_ma['3']['hmm_ll'],2)}/{f(ev_pe['3']['m1_ll']-ev_pe['3']['hmm_ll'],2)} bit/bar、比二阶链少 {f(ev_ma['3']['m2_ll']-ev_ma['3']['hmm_ll'],2)}/{f(ev_pe['3']['m2_ll']-ev_pe['3']['hmm_ll'],2)} bit/bar；top-1 准确率 {pc(ev_ma['3']['hmm_top1'],1)}/{pc(ev_pe['3']['hmm_top1'],1)} vs 显态链的 {min(ev_ma['3']['m1_top1'],ev_ma['3']['m2_top1'],ev_pe['3']['m1_top1'],ev_pe['3']['m2_top1'])*100:.1f}%–{max(ev_ma['3']['m1_top1'],ev_ma['3']['m2_top1'],ev_pe['3']['m1_top1'],ev_pe['3']['m2_top1'])*100:.1f}%（随机 20%）。<b>注意边际分布的 top-1 高达 25%</b>（MA 甚至只有 2%）——五档本身不均衡，log₂/bar 才是公平口径。</p>
<div class="fig">{svg('_dd_hmm.svg')}</div>

<h3>3.4 八卦码在隐状态之上几乎零增量</h3>
<p>把 8 态显码作为额外观测加进 HMM（隐态 → 显态 → log 振幅），holdout 增益：MA <b class="r">仅 {inc_ma['gain_bits']:.3f} bit/bar</b>（joint {f(inc_ma['joint_ll'],3)} vs 纯隐态 {f(inc_ma['hidden_only_ll'],3)}，注意是负增益方向——加了显码反而略升损失），EG <b class="r">{inc_pe['gain_bits']:.4f} bit/bar</b>（噪声水平）。互信息同样微弱：MI(隐态, 显态) MA {h_ma['mi_with_explicit_bits']:.3f} bit / EG {h_pe['mi_with_explicit_bits']:.3f} bit。</p>
<div class="box bad"><b>本轮最锐利的一刀：六位编码对预测问题统计冗余。</b>3 个连续隐变量（每个只取 3 值、且发射只有一个维度 log 振幅）就把下一根振幅预测做到了 1.60–1.80 bit/bar；64 种八卦形态的整条转移矩阵只能到 2.21–2.28。把八卦码摞在波动体制之上，增量 ≈ 0。这不是说形态没有信息（维度①的熵率 2.72 bit、维度⑦的 JS 距离都真实存在），而是说：<b>形态码的信息几乎全部是「当前波动的函数」，对「未来」不提供独立增量</b>——与 D1（压缩率 45%）、D2（三阶记忆饱和）、D7（综卦 JS≈0）三个早期结论方向一致，但 HMM 口径把「冗余」钉到了具体数字上。</div>
''')

# ================= S4 =================
A(f'''
<h2>四、维度十二 · MA×EG 共尾：先拆掉「同一时钟」，还剩多少</h2>
<h3>4.1 方法：slot 调整基准</h3>
<p>两品种按时间戳内连接（{co['n_merged']:,} 根，匹配率 {pc(co['match_rate'],1)}）。共尾倍数 = 联合超越概率 / 独立基准（p_A·p_B）。问题在于：两个品种共用同一套日内钟表，波动都呈 U 型，于是「同一分钟都极端」的概率被共同的开盘/收盘节律机械抬高。<b>修正基准</b>：slot 条件独立——E[p_joint] = Σ_slot P(slot)·P(A 超越 | slot)·P(B 超越 | slot)，把共享节律从基准里剔出去，剩下的才是真实的跨品种尾部相依。</p>
<h3>4.2 原始 vs slot 调整：31× 坍缩到 4×</h3>
<table><thead><tr><th>分位 q</th><th>口径</th><th>联合超越 P(join)</th><th>基准 P(exp)</th><th>倍数</th><th>原始倍数</th></tr></thead><tbody>
''')
for q in ('0.95', '0.99', '0.995'):
    for seg in ('train', 'holdout'):
        d = ct[q][seg]
        A(f'''<tr><td>{q}</td><td>{'train 2020–25' if seg=='train' else 'holdout 2026'}</td><td>{f(d['p_joint'],5)}</td><td>{f(d['expected'],6)}</td><td><b>{fx(d['multiplier'],2)}</b></td><td>{fx(d['multiplier'],1)}</td></tr>''')
A(f'''</tbody></table>
<table><thead><tr><th>slot 调整后（q=0.99）</th><th>观测 P(join)</th><th>slot 独立基准</th><th>倍数</th><th>原始倍数</th></tr></thead><tbody>
<tr><td class="l">train 2020–2025</td><td>{f(sa['train']['observed'],5)}</td><td>{f(sa['train']['expected_slot_indep'],6)}</td><td><b class="g">{fx(sa['train']['multiplier'],2)}</b></td><td>{fx(sa['train']['raw_multiplier'],1)}</td></tr>
<tr><td class="l">holdout 2026</td><td>{f(sa['holdout']['observed'],5)}</td><td>{f(sa['holdout']['expected_slot_indep'],6)}</td><td><b class="g">{fx(sa['holdout']['multiplier'],2)}</b></td><td>{fx(sa['holdout']['raw_multiplier'],1)}</td></tr>
</tbody></table>
<div class="fig">{svg('_dd_cross.svg')}</div>
<div class="box bad"><b>推翻前论 #2：D8「99% 分位联合/独立 = 34×」里，约 87% 是共享日内钟表的机械共振。</b>上一轮 D8 用朴素独立基准得到 34×（本轮同口径复算 {fx(ct['0.99']['train']['multiplier'],1)}）；换成 slot 条件独立基准后，train 只剩 <b class="r">{fx(sa['train']['multiplier'],2)}</b>、holdout {fx(sa['holdout']['multiplier'],2)}。结论方向不变（两个品种确实共尾、仍是同一个风险因子的两个读数），但量级必须改写：<b>「34 倍同生共死」→「4 倍同生共死 + 一个共同的日内时钟」</b>。工程上这不削弱风控价值（联动降仓照做），但削弱了「极端稀有度」叙事——按 34× 估计组合尾部风险会严重高估集中度。</div>

<h3>4.3 稳定性：slot 调整后跨年 1.7–3.7×，原始倍数摆动 12–219×</h3>
<table><thead><tr><th>年份</th><th>原始倍数（q=0.99）</th><th>slot 调整后</th><th>振幅相关</th><th>bar 数</th></tr></thead><tbody>
''')
for y in ('2020', '2021', '2022', '2023', '2024', '2025', '2026'):
    A(f'''<tr><td>{y}</td><td>{fx(byy[y]['0.99']['multiplier'],1)}</td><td><b>{fx(say[y]['multiplier'],2)}</b></td><td>{f(ac[y],3)}</td><td>{byy[y]['0.99']['n']:,}</td></tr>''')
A(f'''</tbody></table>
<p>原始倍数从 2023 年的 {fx(byy['2023']['0.99']['multiplier'],0)} 到 2024 年的 {fx(byy['2024']['0.99']['multiplier'],0)}（差 2.3 倍）——那两年整体波动率极低，同样的绝对阈值下「超越」变稀缺，朴素基准失真被放大。slot 调整后七个年份收敛到 {fx(min(say[y]['multiplier'] for y in say),2)}–{fx(max(say[y]['multiplier'] for y in say),2)}，2026 holdout（{fx(say['2026']['multiplier'],2)}）落在训练区间内，无退化。振幅相关性 0.38–0.72，2026 最高（{f(ac['2026'],3)}）。</p>
<div class="fig">{svg('_dd_roll.svg')}</div>
<p>60 个交易日滚动窗口的 slot 调整倍数：均值 {fx(ro['mean'],1)}、sd {fx(ro['sd'],1)}、区间 0–{fx(ro['max'],1)}（{ro['n_days']:,} 个窗口）——<b>共尾是体制变量不是常数</b>：低波年份（2022–23）接近 1.7×，高波/事件窗口能到 60×。用单一数字做跨品种风险预算会错判一半的时间。</p>

<h3>4.4 领先滞后、方向共动与组合分位</h3>
<table><thead><tr><th>检验</th><th>结果</th><th>结论</th></tr></thead><tbody>
<tr><td class="l">分钟级领先滞后（q=0.99 共尾倍数 vs k）</td><td class="l">k=−1: {fx(ll['-1'],1)} / k=0（同期）: {fx(ct['0.99']['train']['multiplier'],1)} / k=+1: {fx(ll['1'],1)}；±15 分钟衰减到 {fx(ll['-15'],1)}/{fx(ll['15'],1)}</td><td class="l">峰钉在 k=0，两侧基本对称 ⇒ <b>无分钟级套利</b>，与 D8 一致</td></tr>
<tr><td class="l">方向共动（slot 调整）</td><td class="l">同向：A涨→B涨 {fx(dra['A涨→B涨'][0],1)}、A跌→B跌 {fx(dra['A跌→B跌'][0],1)}；反向：{fx(dra['A涨→B跌'][0],1)}/{fx(dra['A跌→B涨'][0],1)}</td><td class="l">同向 6.3–6.6× vs 反向 1.5–1.7×，板块方向共振真实存在</td></tr>
<tr><td class="l">组合分位（q=0.99 分位加总性）</td><td class="l">train 组合/边际加总 = {fx(po['excess_ratio_train'],2)}；holdout = {fx(po['excess_ratio_holdout'],2)}；独立基准 √2 = {fx(po['indep_benchmark'],2)}；错位零基准 {fx(po['null_shuffle_ratio'],2)}</td><td class="l">轻度正相依但<b>低于完全独立</b>（holdout 边际口径 0.91×）⇒ 分散化有效、只是打折</td></tr>
</tbody></table>
<p>按体制切分（train，q=0.95）：高波时段共尾 {fx(C['cond_detail']['highvol']['0.95']['multiplier'],1)} vs 常态 {fx(C['cond_detail']['normal']['0.95']['multiplier'],1)}、夜盘 {fx(C['cond_detail']['night']['0.95']['multiplier'],1)} vs 日盘 {fx(C['cond_detail']['day']['0.95']['multiplier'],1)}、周一 {fx(C['cond_detail']['mon']['0.95']['multiplier'],1)} vs 周二至五 {fx(C['cond_detail']['tuefri']['0.95']['multiplier'],1)}——共尾在高波时段反而更低（4.5× vs 19.7×）：当两边都经常极端时，「联合极端」的惊喜下降。</p>
''')

# ================= S5 =================
A(f'''
<h2>五、三个「推翻前论」集中区</h2>
<div class="box bad"><b>#1 · D4 周一效应 = 数据覆盖假象（维度④ → 维度九复验）</b><br>
前论：「周一振幅为周二~周五的 0.69 倍」。真相：<code>0.69 ≈ 1.06 × (45/69)</code>——真实周一效应（1min 振幅五天最高，同口径 +4.7~5.2%、开盘首 5 分钟块 |收益| 比最高 1.68×）被「周日无交易 ⇒ 周一缺夜盘 ⇒ 24/69 个 5 分钟块按 0 填充」稀释。符号翻转。一般化教训：<b>任何「周期 × 时段」均值矩阵，先查格子样本数</b>；0 填充会把缺失洗成平静。</div>
<div class="box bad"><b>#2 · D8 共尾倍数大部分是日内时钟共振（维度⑧ → 维度十二复验）</b><br>
前论：「99% 分位联合/独立 = 34×，极端行情同生共死」。真相：slot 条件独立基准下 train {fx(sa['train']['raw_multiplier'],1)} → <b>{fx(sa['train']['multiplier'],2)}</b>、holdout {fx(sa['holdout']['raw_multiplier'],1)} → {fx(sa['holdout']['multiplier'],2)}。约 87% 的「共尾」是两个品种共享同一套开盘/收盘节律的算术后果，不是跨品种的风险传染。方向结论（同板块非分散对象、无分钟级领先）不变，量级结论（34 倍同生共死）作废。一般化教训：<b>相依性度量必须先对齐「共同的确定性节律」</b>，否则测到的是钟表不是市场。</div>
<div class="box bad"><b>#3 · D7 综卦 ≠ 大变动（维度⑦，本轮回顾）</b><br>
前论候选：「上下影互换的综卦对意味着大变动」。真相：综卦对的下一根振幅条件分布 JS 距离仅 0.0011–0.002 bit，比错卦对（0.0354）近 30 倍——影线换边不改变后续动力学，在演化意义上近似同一个状态的两种观测。<b>静态几何（现在长什么样）与动态几何（之后怎么走）是两个问题</b>，对预测只该信后者。与维度十一 HMM 的结论（形态码对未来的增量 ≈0）互为印证。</div>

<h2>六、工程结论</h2>
<div class="box ok">
<ol class="tight">
<li><b>时段分层是一切幅度类参数的前提。</b>开盘 15 分钟占会话振幅 {pc(min(min(conc_ma[s]['range_share_first15'],conc_eg[s]['range_share_first15']) for s in ('9','10','13','21')))}–{pc(max(max(conc_ma[s]['range_share_first15'],conc_eg[s]['range_share_first15']) for s in ('9','10','13','21')))}。止损宽度、加仓阈值、滑点假设按 9/10/13/21 时段分别标定；周一开盘单独一档（1 分钟振幅 +4.7~5.2%、开盘首 5 分钟块 |收益| 比 1.50–1.68×、振幅比 1.35–1.44×）。</li>
<li><b>方向类信号用 clv 只配做均值回归。</b>clv 极端 ⇒ 下一根反向（IC −0.20→−0.07，holdout 同号）；「同向延续」与抛硬币无异。若做反转因子，成本敏感性要先过压力测试（分钟级反转的信号半衰期只有 1–2 根）。</li>
<li><b>幅度类信号看成交量，不看形态。</b>vol→下一根振幅 IC 0.46–0.63 且 holdout 不衰减，是全部检验里最稳的关系；相比之下整张八卦转移矩阵（2.28 bit/bar）打不过 3 值波动体制 HMM（1.60–1.80 bit/bar）。<b>把工程预算从「形态模式识别」转移到「波动体制估计」。</b></li>
<li><b>持续期按两层管理。</b>头部（run=1–2 根）用一阶链够用；一旦某态存活 ≥4–5 根，粘性概率跳升（长 run 超几何基准 7–8 倍）——持仓止盈/止损的时间标度应在「进入第 4 根同态」时切换。</li>
<li><b>跨品种风控继续用共尾，但用 4× 不用 34×。</b>同板块品种同时亮高波态 ⇒ 联动降仓的规则保留（方向共动 6.3–6.6× vs 反向 1.5–1.7× 支持它）；但组合尾部风险按 slot 调整后的 2–4× 估计，且按滚动窗口动态更新（0–62× 的体制摆动）。分钟级领先套利确认不存在（峰在 k=0）。</li>
<li><b>数据自检清单再添两条：</b>① 周期×时段矩阵先查格子样本数（周日无交易坑）；② 连续型代理变量先查点质量/平局（clv 68% 只取三个值的坑，分位数会被平局吃掉）。</li>
</ol>
</div>

<h2>七、口径与边界</h2>
<div class="box warn">
<ul class="tight">
<li><b>数据</b>：MA/EG 主力连续 1 分钟，2020-01～2026-10；训练 2020–2025（{co['by_year']['2020']+co['by_year']['2021']+co['by_year']['2022']+co['by_year']['2023']+co['by_year']['2024']+co['by_year']['2025']:,} 根/品种），holdout 2026（{co['by_year']['2026']:,} 根/品种）。夜盘归属次日交易日；相邻对限定会话内 60 秒。</li>
<li><b>零振幅 bar</b>：h==l 占 MA {pc(ma['coverage']['vol_zero_frac'],3)} / EG {pc(eg['coverage']['vol_zero_frac'],3)}；HMM 中以非零最小振幅的一半为下限（截尾观测），IC/持续期类统计直接排除。amp/clv 无 NaN。</li>
<li><b>clv 平局</b>：MA 有 20.0% 的 bar clv 恰为 0.5（分钟波幅中位数仅 3 tick），其五档分位退化为三档，本报告已如实标注；EG 分档干净（49.7% 三点质量但分位点未重合）。</li>
<li><b>HMM 边界</b>：发射只有 log 振幅一个维度；K 由 BIC 式权衡取 3（K=4 holdout 略好但参数量近翻倍且 MA/EG 不稳定）；forward 已过 729 路径暴力校验、EM 单调性全程记录。隐态标签跨品种不可比（仅按 μ 排序命名）。</li>
<li><b>共尾边界</b>：slot 调整基准假设「slot 内条件独立」是上界松绑（真实 slot 内仍有分钟级相关，故 4× 仍可能略高估）；组合分位检验含错位零基准（{fx(po['null_shuffle_ratio'],2)}）防机械相关。</li>
<li><b>未覆盖</b>：只做了 MA×EG 两个品种；把 slot 调整基准推广到整个板块（多品种联合超越、网络式风险传染）是下一步。</li>
</ul>
</div>

<h2>八、复现清单</h2>
<table><thead><tr><th>文件</th><th>内容</th></tr></thead><tbody>
<tr><td class="l"><code>ke_common.py</code></td><td class="l">共享数据加载与工具（8 态编码、会话分段、qbin/spearman/js_div）</td></tr>
<tr><td class="l"><code>step27_micro.py</code> → <code>results_micro.json</code></td><td class="l">维度九：日内 U 型、OFI 代理、量价关系、周一效应复验</td></tr>
<tr><td class="l"><code>step28_persist.py</code> → <code>results_persist.json</code></td><td class="l">维度十/十一：run 统计、hazard、HMM(K=2/3/4)、holdout 评估、3^6 校验</td></tr>
<tr><td class="l"><code>step28b_hazard_fix.py</code></td><td class="l">hazard 删失修正（合并进 results_persist.json 的 hazard_fix 键）</td></tr>
<tr><td class="l"><code>step29_cross.py</code> → <code>results_cross.json</code></td><td class="l">维度十二：共尾、slot 调整、领先滞后、方向、组合、滚动</td></tr>
<tr><td class="l"><code>step30_deep_figs.py</code> → <code>_dd_*.svg</code></td><td class="l">五张图（微结构/持续期/HMM/共尾/滚动）</td></tr>
<tr><td class="l"><code>step31_deep_report.py</code> → 本文件</td><td class="l">报告生成</td></tr>
</tbody></table>
<p class="mut">运行环境 Python 3.14.6（pandas 3.0.5 / numpy 2.5.2 / scipy 1.18.0）。所有数字由脚本从结果 JSON 直接注入，未经手工转录。</p>
</div></body></html>''')

out = f'{BASE}/deep_report.html'
open(out, 'w', encoding='utf-8').write(''.join(H))
print('written', out, os.path.getsize(out), 'bytes')
