# -*- coding: utf-8 -*-
"""step63 — 生成 THS 校正网格 V5X 重跑报告 (HTML)"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

R = json.load(open('results_ths.json', encoding='utf-8'))
RB = json.load(open('results_ths_breadth.json', encoding='utf-8'))
ORIG = json.load(open('results_step46.json', encoding='utf-8'))

ths_v5x = R['THS']['V5X']; ba_v5x = R['BA']['V5X']; orig_v5x = ORIG.get('V5X', {})
golds = R['gold_syms']

# 逐品种 (gold-13)
df_t = pd.read_csv('trades_ths_THS_V5X.csv')
df_b = pd.read_csv('trades_ths_BA_V5X.csv')
def ps(df):
    if df is None or len(df)==0: return {}
    g=df.groupby('sym')
    return {s: dict(n=int(len(x)), net=float(x.net.sum()), win=float((x.net>0).mean()),
                    pf=(float(x.loc[x.net>0,'net'].sum()/max(1e-9,-x.loc[x.net<0,'net'].sum())) if (x.net<0).any() else float('nan')))
            for s,x in g}
pt, pb = ps(df_t), ps(df_b)

def fmt(x, d=0):
    if x is None: return '-'
    return f'{x:,.{d}f}'

# 逐品种表格
rows=''
allg = sorted(set(list(pt)+list(pb)))
for s in allg:
    a=pt.get(s,{}); b=pb.get(s,{})
    cls = ' class="pos"' if a.get('net',0)>0 else ' class="neg"'
    rows += (f'<tr><td>{s}</td>'
             f'<td>{a.get("n",0)}</td><td{cls}>{fmt(a.get("net",0))}</td><td>{a.get("pf",0):.2f}</td><td>{a.get("win",0):.2f}</td>'
             f'<td>{b.get("n",0)}</td><td>{fmt(b.get("net",0))}</td><td>{b.get("pf",0):.2f}</td><td>{b.get("win",0):.2f}</td></tr>')

# 板块广度
cl = RB.get('by_cluster', {})
crows=''
for k,v in sorted(cl.items(), key=lambda kv:-kv[1].get('net',0)):
    crows += f'<tr><td>{k}</td><td>{v["n"]}</td><td class="{"pos" if v["net"]>0 else "neg"}">{fmt(v["net"])}</td><td>{v["win"]:.2f}</td></tr>'

def metblock(m, label):
    tr=m.get('train',{}); ho=m.get('holdout',{})
    return (f'<div class="card"><h3>{label}</h3>'
            f'<p>笔数 <b>{m.get("n")}</b> ｜ 净利 <b class="{"pos" if m.get("net_yuan",0)>0 else "neg"}">{fmt(m.get("net_yuan"))}</b> ｜ '
            f'PF <b>{m.get("pf"):.3f}</b> ｜ 胜率 <b>{m.get("win"):.3f}</b> ｜ MDD <b class="neg">{fmt(m.get("mdd_yuan"))}</b></p>'
            f'<p class="sub">训练(2020-2025) 净利 {fmt(tr.get("net_yuan"))} (PF {tr.get("pf"):.3f}, {tr.get("n")}笔) ｜ '
            f'OOS(2026) 净利 <b class="{"pos" if ho.get("net_yuan",0)>0 else "neg"}">{fmt(ho.get("net_yuan"))}</b> (PF {ho.get("pf"):.3f}, {ho.get("n")}笔)</p></div>')

html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>THS 校正网格 · V5X 重跑报告</title>
<style>
 body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:1040px;margin:0 auto;padding:28px 22px;color:#1f2733;background:#f6f8fb;line-height:1.6}}
 h1{{font-size:23px;margin:0 0 4px}} h2{{font-size:18px;margin:30px 0 10px;border-left:4px solid #2f6df0;padding-left:10px}}
 h3{{font-size:14px;margin:0 0 6px;color:#33405a}}
 .lead{{color:#5b6675;font-size:13px;margin:6px 0 18px}}
 .card{{background:#fff;border:1px solid #e3e8f0;border-radius:10px;padding:12px 16px;margin:8px 0;box-shadow:0 1px 3px rgba(20,40,80,.04)}}
 .card .sub{{color:#7a8694;font-size:12px;margin:4px 0 0}}
 .pos{{color:#c0392b;font-weight:600}} .neg{{color:#1e8e3e;font-weight:600}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;font-size:13px;margin:8px 0}}
 th,td{{padding:7px 9px;text-align:right;border-bottom:1px solid #eef1f6}}
 th{{background:#2f3a4d;color:#fff;font-weight:600}} td:first-child,th:first-child{{text-align:left}}
 tr:hover{{background:#f3f6fc}}
 .kpi{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}}
 .kpi .card{{flex:1;min-width:150px}}
 .warn{{background:#fff6e6;border:1px solid #f3d99a;border-radius:10px;padding:12px 16px;margin:14px 0;font-size:13px}}
 .tag{{display:inline-block;background:#e8f0ff;color:#2f6df0;border-radius:5px;padding:1px 7px;font-size:11px;margin-right:4px}}
 footer{{color:#90a0b3;font-size:11px;margin-top:26px;border-top:1px solid #e3e8f0;padding-top:10px}}
</style></head><body>

<h1>同花顺校正网格 · V5X 融合策略重跑报告</h1>
<p class="lead">数据：<span class="tag">prod50/bar_60m_ths.csv.gz</span> 同花顺连续60交易分钟网格（信号与图表一致）。
对比基准：原云端整数整点桶网格 <span class="tag">bar_60m_ba</span>。
唯一变量=网格口径；其余(EMA140/ATR14/ADX14/MACD/门控/成本/会员因子)完全复用 step46。</p>

<h2>一、核心结论</h2>
<div class="kpi">
 {metblock(ths_v5x,'THS 校正网格 · V5X (13个全额历史品种)')}
 {metblock(ba_v5x,'原整点桶网格 · V5X (同一13品种, 控制组)')}
 {metblock(orig_v5x,'原整点桶网格 · V5X (原全56品种回测)')}
</div>
<p><b>结论：</b>在「与实盘图表一致」的同花顺网格上，V5X 融合策略在化工/IF 焦点组（13个有全额历史的品种）上
<b>接近平仓</b>（净利 {fmt(ths_v5x['net_yuan'])}，PF {ths_v5x['pf']:.3f}），而原整点桶网格在同组上净亏 {fmt(ba_v5x['net_yuan'])}。
原全56品种 +{fmt(orig_v5x['net_yuan'])} 的"亮眼"收益<b>主要来自另外 43 个品种</b>——而这 43 个品种当前只有 2026 年 Sina 窗口的 THS 历史，
无法在正确网格上做完整样本外验证（见第四节）。
换言之，旧网格对图表的"信号"相当部分是<b>网格错配伪像</b>；校正后，方向性策略在焦点组上并未显现真实超额收益。</p>

<div class="warn"><b>重要口径说明：</b>
① IF888 在 dim_trading_cost 中缺费率条目，两网格均按相同规则剔除 → A/B 公平。
② 焦点组 13 品种（原14金标准剔除IF）THS 均有 2020+ 全历史；其余 43 品种 THS 仅 ~1023 根（2026 窗口），低于 1500 根阈值，不进主回测。
③ 旧全56结果来自 results_step46.json，作为上下文对照，非同口径直接比较。</div>

<h2>二、V5X 逐品种拆解（焦点组 13 品种）</h2>
<table><thead><tr><th>品种</th>
<th colspan="4">THS 校正网格</th><th colspan="4">原整点桶网格</th></tr>
<tr><th></th><th>笔数</th><th>净利</th><th>PF</th><th>胜率</th><th>笔数</th><th>净利</th><th>PF</th><th>胜率</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class="lead">校正网格彻底重排了"哪些品种被交易"：EB/EG/SA 在旧网格零交易、校正后有显著仓位；FG/L/MA/PP/TA/V 旧网格有交易、校正后盈亏结构也逆转。
这说明旧网格的逐品种信号本身就很脆弱。</p>

<h2>三、2026 窗口广度回测（全部 60 品种 · THS 网格）</h2>
<div class="card"><p>总笔数 <b>{RB['total']['n']}</b> ｜ 净利 <b class="{"pos" if RB['total']['net_yuan']>0 else "neg"}">{fmt(RB['total']['net_yuan'])}</b>
｜ PF <b>{RB['total']['pf']:.3f}</b> ｜ 胜率 <b>{RB['total']['win']:.3f}</b> ｜ MDD <b class="neg">{fmt(RB['total']['mdd_yuan'])}</b></p>
<p class="sub">候选 {RB['n_sym_total']} 品种，实际有交易 {RB['n_sym_traded']} 个。43 个非金标准品种仅 2026 窗口、且 200 日预热后仅剩 1–2 月可用，故本表只反映 2026 末段 OOS 方向性。</p></div>
<table><thead><tr><th>板块</th><th>笔数</th><th>净利</th><th>胜率</th></tr></thead><tbody>{crows}</tbody></table>

<h2>四、未决项与下一步</h2>
<p>要在「正确网格」上复现原全56品种 +{fmt(orig_v5x['net_yuan'])} 的回测，必须把<b>另外 43 个品种也从 1 分钟原始 K 线重建为同花顺 60m 网格</b>
（金标准 14 品种已做，其余尚未）。这一步是独立的数据工程任务，完成后即可跑完整 THS 全样本 V5X，给出与原回测真正同口径的结论。</p>
<p class="lead">已有交付物：trades_ths_THS_V5X.csv（校正网格逐笔）、trades_ths_BA_V5X.csv（控制组逐笔）、
trades_ths_breadth_V5X.csv（广度逐笔）、results_ths.json / results_ths_breadth.json（指标）。</p>

<footer>生成：step63_ths_report.py ｜ 网格：prod50/bar_60m_ths.csv.gz ｜ 引擎：fusion_v5x_engine / step46_prod_bt ｜ 复权口径与云端 bar_60m_ba 对齐。</footer>
</body></html>'''

open('ths_rerun_v5x_report.html','w',encoding='utf-8').write(html)
print('saved ths_rerun_v5x_report.html  (', len(html), 'bytes )')
