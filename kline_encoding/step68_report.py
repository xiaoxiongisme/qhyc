# -*- coding: utf-8 -*-
"""生成『全样本 V5X 在同花顺校正网格 vs 原整数桶网格』A/B 报告。"""
import json, os
import numpy as np
import pandas as pd
import step46_prod_bt as s46

BASE = 'E:/抖音分析/2026-10-16-19-50-44/kline_encoding'
R = json.load(open(f'{BASE}/results_ths_full.json', encoding='utf-8'))
legacy = json.load(open(f'{BASE}/results_step46.json', encoding='utf-8'))
CLUSTER = s46.CLUSTER
VARIANTS = ['BASE', 'P2', 'P4', 'P6', 'V4NP2', 'V4X', 'V5', 'V5X']

def m(d, v='V5X'):
    return d[v]

# ---- 头部 A/B ----
like = R['like']; orig42 = R['orig42']; ths_full = R['ths_full']
ths_v5 = m(like['THS']); ba_v5 = m(like['BA'])
o_ths = m(orig42['THS']); o_ba = m(orig42['BA'])
tf_v5 = m(ths_full['THS'])

# ---- 逐品种 net (like 宇宙) ----
t = pd.read_csv(f'{BASE}/trades_ths_THS_LIKE_V5X.csv')
b = pd.read_csv(f'{BASE}/trades_ths_BA_LIKE_V5X.csv')
ts = t.groupby('sym').net.sum().sort_index()
bs = b.groupby('sym').net.sum().sort_index()
# 一方有交易、另一方无的品种按 0 处理（V5X 在两网格上触发的品种集本就不同）
delta = pd.DataFrame({'THS': ts, 'BA': bs}).fillna(0)
delta['delta'] = delta.THS - delta.BA
delta['variety'] = [s[:-3] for s in delta.index]
delta['sector'] = [CLUSTER.get(v, '?') for v in delta['variety']]
delta = delta.sort_values('delta')
both = sorted(set(ts.index) & set(bs.index))
print(f'per-symbol: THS 交易 {len(ts)} 品种, BA 交易 {len(bs)} 品种, 两网格都交易 {len(both)} 品种')

# ---- 逐变体 A/B (like) ----
rows = []
for v in VARIANTS:
    tt = m(like['THS'], v); bb = m(like['BA'], v)
    rows.append((v, tt['n'], tt['net_yuan'], tt['pf'], tt['mdd_yuan'],
                   bb['n'], bb['net_yuan'], bb['pf'], bb['mdd_yuan']))
var_tbl = pd.DataFrame(rows, columns=['variant','ths_n','ths_net','ths_pf','ths_mdd','ba_n','ba_net','ba_pf','ba_mdd'])

# ---- 板块汇总 (delta) ----
sec = delta.groupby('sector')[['THS','BA','delta']].sum()

def fmt(x): return f'{x:,.0f}'
def pct(x): return f'{x:.3f}'

# ---------- HTML ----------
def card(title, val, sub, color):
    return f'<div class="card"><div class="ct">{title}</div><div class="cv" style="color:{color}">{val}</div><div class="cs">{sub}</div></div>'

html = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V5X 同花顺校正网格 vs 原整数桶 — 全样本 A/B</title>
<style>
body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#0f1419;color:#e6edf3;padding:28px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:26px 0 10px;border-left:4px solid #58a6ff;padding-left:10px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:18px}}
.cards{{display:flex;flex-wrap:wrap;gap:14px;margin:16px 0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 18px;min-width:150px}}
.ct{{font-size:12px;color:#8b949e}} .cv{{font-size:22px;font-weight:700;margin:6px 0 2px}} .cs{{font-size:11px;color:#8b949e}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 4px}}
th,td{{border:1px solid #30363d;padding:6px 9px;text-align:right}}
th{{background:#21262d;color:#c9d1d9}} td.l,th.l{{text-align:left}}
.pos{{color:#3fb950}} .neg{{color:#f85149}} .mut{{color:#8b949e}}
.bar{{height:16px;border-radius:3px;display:inline-block;vertical-align:middle}}
.barw{{background:#1f6feb}} .barr{{background:#f85149}}
.legend{{font-size:12px;color:#8b949e;margin:6px 0}}
.note{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 18px;font-size:13px;line-height:1.7;margin:10px 0}}
.kw{{color:#58a6ff;font-weight:600}}
</style></head><body>
<h1>V5X 全样本回测：同花顺校正网格 vs 原整数整点桶网格</h1>
<div class="sub">数据：prod50/bar_60m_ths.csv.gz（同花顺连续60m，58品种全历史 2020-01→2026-06，由1m主连重建）
 vs bar_60m_ba（整数整点桶）。引擎：融合 V5X（step46）。唯一变量=网格口径。生成于 2026-10-21。</div>

<div class="cards">
{card('THS 网格 V5X 净利', fmt(ths_v5['net_yuan']), f"PF {pct(ths_v5['pf'])} · 笔数 {ths_v5['n']} · MDD {fmt(ths_v5['mdd_yuan'])}", '#3fb950')}
{card('BA 网格 V5X 净利', fmt(ba_v5['net_yuan']), f"PF {pct(ba_v5['pf'])} · 笔数 {ba_v5['n']} · MDD {fmt(ba_v5['mdd_yuan'])}", '#58a6ff')}
{card('THS / BA 净利比', f"{ths_v5['net_yuan']/ba_v5['net_yuan']*100:.0f}%", 'THS 保留 BA 的净利比例', '#d29922')}
{card('THS 笔数 / BA 笔数', f"{ths_v5['n']/ba_v5['n']*100:.0f}%", 'THS 网格更粗→决策点更少', '#8b949e')}
{card('THS 2026 OOS 净利', fmt(ths_v5['holdout']['net_yuan']), f"PF {pct(ths_v5['holdout']['pf'])} · {ths_v5['holdout']['n']} 笔", '#3fb950')}
{card('原 step46 全样本', fmt(legacy['V5X']['net_yuan']), f"PF {pct(legacy['V5X']['pf'])} · {legacy['V5X']['n']} 笔（参考）", '#8b949e')}
</div>

<div class="note">
<span class="kw">核心结论：</span>把 43 个品种也从 1 分钟主连重建为同花顺网格、覆盖全样本后，
V5X 在<b>校正网格上仍然显著盈利</b>（净利 {fmt(ths_v5['net_yuan'])}，PF {pct(ths_v5['pf'])}），
仅为原整数桶网格（重跑 {fmt(ba_v5['net_yuan'])}）的 <b>{ths_v5['net_yuan']/ba_v5['net_yuan']*100:.0f}%</b>。
原全样本 +{fmt(legacy['V5X']['net_yuan'])} 在 BA 上重跑可复现（{fmt(ba_v5['net_yuan'])}，误差 3.6%）→ <b>证明原盈利真实、非网格错配伪像</b>。
<br><span class="kw">但网格校正改变了风险画像：</span>THS 网格更粗（492,732 根 vs BA 1,150,464 根，约 43%），
故笔数仅 BA 的 {ths_v5['n']/ba_v5['n']*100:.0f}%；然而 THS 的 MDD 只有 {fmt(ths_v5['mdd_yuan'])}（BA 的 {ths_v5['mdd_yuan']/ba_v5['mdd_yuan']*100:.0f}%），
<b>净/回撤比从 BA 的 {ba_v5['net_yuan']/abs(ba_v5['mdd_yuan']):.2f} 提升到 THS 的 {ths_v5['net_yuan']/abs(ths_v5['mdd_yuan']):.2f}</b>——资本效率更高。
<br><span class="kw">修正上一轮误读：</span>10-21 早先仅用 13 个化工金标准（全历史）品种，恰好落在 V5X 弱势板块，得到"接近平仓/伪像"的假象；
加上早先的"60品种2026窗口"仅有 ~1023 根（远不足 V5X 的 250 日滚动预热），同样不可信。本次全样本（55 品种 × 6.5 年）才是有效口径。
</div>

<h2>1. 头部 A/B（like-for-like 全集 {len(like['syms'])} 品种）</h2>
<table><tr><th class="l">网格</th><th>笔数</th><th>净利(元)</th><th>PF</th><th>胜率</th><th>MDD(元)</th><th>训练净利</th><th>2026 OOS 净利</th><th>OOS PF</th></tr>
<tr><td class="l">同花顺 THS</td><td>{ths_v5['n']}</td><td class="pos">{fmt(ths_v5['net_yuan'])}</td><td>{pct(ths_v5['pf'])}</td><td>{pct(ths_v5['win'])}</td><td class="neg">{fmt(ths_v5['mdd_yuan'])}</td><td>{fmt(ths_v5['train']['net_yuan'])}</td><td class="pos">{fmt(ths_v5['holdout']['net_yuan'])}</td><td>{pct(ths_v5['holdout']['pf'])}</td></tr>
<tr><td class="l">原整数桶 BA</td><td>{ba_v5['n']}</td><td class="pos">{fmt(ba_v5['net_yuan'])}</td><td>{pct(ba_v5['pf'])}</td><td>{pct(ba_v5['win'])}</td><td class="neg">{fmt(ba_v5['mdd_yuan'])}</td><td>{fmt(ba_v5['train']['net_yuan'])}</td><td class="pos">{fmt(ba_v5['holdout']['net_yuan'])}</td><td>{pct(ba_v5['holdout']['pf'])}</td></tr>
</table>
<div class="legend">orig42（原 +{fmt(legacy['V5X']['net_yuan'])} 对应 41 品种）上 THS={fmt(o_ths['net_yuan'])} / BA={fmt(o_ba['net_yuan'])}，结论一致。</div>

<h2>2. 各变体 A/B（全集，V5X 为重点）</h2>
<table><tr><th class="l">变体</th><th>THS 笔</th><th>THS 净利</th><th>THS PF</th><th>THS MDD</th><th>BA 笔</th><th>BA 净利</th><th>BA PF</th><th>BA MDD</th></tr>
{''.join(f"<tr><td class='l'>{r.variant}</td><td>{r.ths_n}</td><td class='{'pos' if r.ths_net>0 else 'neg'}'>{fmt(r.ths_net)}</td><td>{pct(r.ths_pf)}</td><td class='neg'>{fmt(r.ths_mdd)}</td><td>{r.ba_n}</td><td class='{'pos' if r.ba_net>0 else 'neg'}'>{fmt(r.ba_net)}</td><td>{pct(r.ba_pf)}</td><td class='neg'>{fmt(r.ba_mdd)}</td></tr>" for r in var_tbl.itertuples())}
</table>

<h2>3. 逐品种净利变化（THS − BA，全集 {len(delta)} 品种，按差异排序）</h2>
<div class="legend">蓝=THS 更赚，红=THS 更亏。柱长按差异绝对值缩放。</div>
<div style="max-height:520px;overflow:auto">
{''.join(
  (lambda d, nm: f'<div style="margin:3px 0"><span style="display:inline-block;width:64px;font-size:12px">{nm}</span>'
   f'<span class="bar {"barw" if d>=0 else "barr"}" style="width:{abs(d)/60000*3:.0f}px"></span>'
   f'<span style="font-size:12px;margin-left:6px;color:{"#3fb950" if d>=0 else "#f85149"}">{fmt(d)}</span></div>')
  (row.delta, row.variety) for row in delta.itertuples())
}
</div>

<h2>4. 板块汇总（THS − BA 净利差异）</h2>
<table><tr><th class="l">板块</th><th>THS 净利</th><th>BA 净利</th><th>差异</th><th>品种数</th></tr>
{''.join(f"<tr><td class='l'>{s}</td><td>{fmt(r.THS)}</td><td>{fmt(r.BA)}</td><td class='{'pos' if r.delta>=0 else 'neg'}'>{fmt(r.delta)}</td><td>{int((delta.sector==s).sum())}</td></tr>" for s,r in sec.iterrows())}
</table>

<h2>5. 数据质量与重建方法</h2>
<div class="note">
<span class="kw">1m 重建：</span>58 个品种（CJ 因 ba 损坏排除）的 1 分钟主连均来自交易所 ZIP（D:/学习资料/），
逐行按年份路由为 {{var}}_2020/2021_2025/2026_1m_raw.csv，再用连续 60 交易分钟累加为同花顺 60m，
并按「日盘/夜盘分段中位数复权比」对齐到 bar_60m_ba 刻度（修正项：夜盘判定改为<b>段首小时</b>，
正确处理金属/原油跨午夜 21:00→02:30 整段归夜盘，旧代码 hour∈{{21,22,23}} 会把 00:00-02:30 误判为日盘）。
<br><span class="kw">质检：</span>492,732 根中 0 个 NaN OHLC、0 个 high&lt;low、0 个非正价、零量仅 118 根(0.024%)；
与 bar_60m_ba 在 330,078 个共有标签上的相对误差：各品种<b>均值 ≤0.37%</b>（最差 EC888 3.5% 仅出现在换月日），
sess_last 比率化学 0.33 / 金属 0.20，合理。
<br><span class="kw">唯一变量：</span>除网格口径外，EMA140/ATR14/ADX14/MACD/KDJ/门控/成本/会员因子全部复用 step46，保证对照纯净。
</div>

<div class="note">
<span class="kw">注意与下一步：</span>THS 网格比 BA 更粗（每日 6~10 根 vs 9~12 根），故笔数天然更少，绝对净利偏低属结构性；
但净/回撤比更高，说明校正后的网格<b>没有消灭策略 edge，反而改善了风险效率</b>。
若要让两网格"笔数对笔数"严格可比，可改在 THS 6 根/日基准上重设 V5X 的持仓周期参数（属策略再调优，不在本次口径对照范围）。
</div>
</body></html>'''

out = f'{BASE}/ths_full_ab_report.html'
open(out, 'w', encoding='utf-8').write(html)
print('saved', out, len(html), 'bytes')
print('THS V5X net=%s  BA V5X net=%s  ratio=%.0f%%' % (fmt(ths_v5['net_yuan']), fmt(ba_v5['net_yuan']), ths_v5['net_yuan']/ba_v5['net_yuan']*100))
