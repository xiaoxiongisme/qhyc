# -*- coding: utf-8 -*-
"""step78 交付报告：数据根因 + THS跨网格复核 + M5方向过滤比对"""
import json

A = json.load(open('step76_data_audit.json', encoding='utf-8'))
V = json.load(open('step77_validation.json', encoding='utf-8'))

def f(x, d=0):
    try:
        return f'{float(x):,.{d}f}'
    except Exception:
        return str(x)

def pct(x):
    try:
        return f'{float(x)*100:.1f}%'
    except Exception:
        return '-'

def mrow(g, fk, label, bold=False):
    m = V[g][fk]['metrics']
    tr = m.get('train', {}); ho = m.get('holdout', {})
    b = ' class="b"' if bold else ''
    return (f'<tr{b}><td{b}>{label}</td>'
            f'<td>{m["n"]}</td><td>{f(m["net_yuan"])}</td><td>{f(m["pf"],3)}</td>'
            f'<td>{f(m["mdd_yuan"])}</td><td>{f(m["mar"],2)}</td>'
            f'<td>{f(tr.get("net_yuan",0))}</td><td>{f(ho.get("net_yuan",0))}</td></tr>')

# 根因表（1m 溯源，摘 ratio 列）
trace = A['trace_1m']
trace_rows = ''
for s, t in trace.items():
    if 'firstday_1m' in t:
        r = t['ratio']
        cls = ' class="bad"' if (r is None or r < 0.8) else ''
        trace_rows += (f'<tr{cls}><td>{s}</td><td>{t["firstday_1m"]}</td>'
                       f'<td>{t["typical_1m"]}</td><td>{r}</td></tr>')
    else:
        trace_rows += f'<tr><td>{s}</td><td colspan=3>1m源缺失(MISSING)</td></tr>'

# 完整性表
integ = A['integrity']
integ_rows = ''
for name, rec in integ.items():
    integ_rows += f'<tr><td>{name}</td><td>{rec.get("rows","-")}</td>' \
                  f'<td>{rec.get("dup_sym_ts","-")}</td>' \
                  f'<td>{rec.get("high_lt_low","-")}</td>' \
                  f'<td>{rec.get("nonpos_close","-")}</td>' \
                  f'<td>{rec.get("zero_vol","-")}</td></tr>'

HTML = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V5X 复核报告 · 2026-10-22</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;margin:0;background:#f4f6f9;color:#1c2733;line-height:1.6;padding:28px}}
.wrap{{max-width:1080px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
h2{{font-size:19px;margin:30px 0 10px;padding-left:10px;border-left:4px solid #2b7de9}}
h3{{font-size:15px;color:#456;margin:18px 0 8px}}
.sub{{color:#7a8a99;font-size:13px;margin-bottom:8px}}
.card{{background:#fff;border:1px solid #e6ebf1;border-radius:10px;padding:16px 18px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
th,td{{border:1px solid #e6ebf1;padding:6px 9px;text-align:right}}
th{{background:#eef3f9;color:#334;font-weight:600}}
td:first-child,th:first-child{{text-align:left}}
tr:nth-child(even) td{{background:#fafcfe}}
tr.b td{{background:#eaf3ff;font-weight:600}}
.bad{{color:#c0392b;font-weight:600}}
.note{{font-size:12.5px;color:#6b7a89;background:#fff8e9;border:1px solid #f0e2bf;border-radius:8px;padding:10px 12px;margin:10px 0}}
.ok{{color:#1f8a4c;font-weight:600}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}}
.kpi{{flex:1;min-width:150px;background:#fff;border:1px solid #e6ebf1;border-radius:10px;padding:12px 14px}}
.kpi .v{{font-size:22px;font-weight:700;color:#2b7de9}}
.kpi .l{{font-size:12px;color:#7a8a99}}
code{{background:#eef3f9;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">
<h1>V5X 策略复核报告</h1>
<div class="sub">2026-10-22 · 数据根因审计 + THS 跨网格复核 + M5 方向过滤替换比对 · 全部改动仅修数据/补丁参数，未改引擎逻辑与表结构</div>

<h2>一、数据「不完全」根因与类似情况（请求#2）</h2>
<div class="card">
<h3>结论：203 根前导 NaN 棒的根因不在后复权</h3>
<ul>
<li>203 根 NaN-OHLC 棒分布在 <b>25 个品种</b>，<b>全部 25 个</b>都落在各自序列最前 <b>≤15 根</b>（前导位置），即每个品种连续序列的「开头几根」。</li>
<li>溯源上游 1 分钟主连源 <code>_sym1m/</code>：约 15/25 品种在「首根 NaN 对应交易日」<b>1m 棒数为 0</b>（该日在上游源中根本不存在），其余（如 EG/PG）仅约 65% 常态量 → 说明 60m 导出在这些<b>上市/换月拼接处的首桶是空桶或只有部分 1m tick</b>。</li>
<li><b>后复权（back-adjustment）只是对已有 60m 棒做价格平移，不会制造也不会填补缺失棒</b>。因此「我多次让你做后复权」与「棒不完全」是两件事、互不相关：复权正确，缺口来自最上游 1m 覆盖不全。</li>
<li>已修复（上一轮）：备份 <code>*.bak_nan</code> 后删除 203 根前导 NaN 棒；<code>bar_60m_ba</code> 1,150,464→1,150,261，<code>bar_60m_raw</code> 1,103,468→1,103,265，残留 NaN=0。</li>
</ul>
</div>

<div class="card">
<h3>上游 1m 溯源（首根 NaN 对应日的 1m 棒数 vs 该品种常态日）</h3>
<div class="note">红色 = 首日前导日 1m 量 &lt; 常态 80%，即上游前导稀疏/空桶，直接造成 60m 聚合出 NaN。</div>
<table><thead><tr><th>品种</th><th>首日前导日 1m</th><th>常态日 1m</th><th>比例</th></tr></thead>
<tbody>{trace_rows}</tbody></table>
</div>

<div class="card">
<h3>跨表完整性审计（当前已清洗文件）</h3>
{integ_rows and f'<table><thead><tr><th>表</th><th>行数</th><th>重复(sym,ts)</th><th>high&lt;low</th><th>非正收盘价</th><th>零量棒</th></tr></thead><tbody>{integ_rows}</tbody></table>'}
<div class="note"><b>新发现的同类/相关数据损坏：</b> <code>bar_60m_ba</code> 含 <b>3,301 根负收盘价棒，全部来自 CJ888（红枣）</b>（均值 −3957，最低 −7605）。负价在期货中不可能，属<b>后复权损坏</b>——印证此前「红枣日线复权序列损坏」的结论已延伸到 60m。CJ888 已在引擎 <code>EXCLUDE</code> 中，回测不受影响；建议保持排除或单独重建其 BA。其余表：无重复 ts、无 high&lt;low、factor_value 的 member_net_z 无 NaN。</div>
</div>

<h2>二、THS 网格同参数跨网格复核（请求#1）</h2>
<div class="card">
<h3>推荐参数（来自上轮扫描，仅补丁全局量）</h3>
<div class="kpis">
  <div class="kpi"><div class="v">SL=3.0</div><div class="l">止损 ATR</div></div>
  <div class="kpi"><div class="v">TRAIL=2.0</div><div class="l">追踪止损 ATR</div></div>
  <div class="kpi"><div class="v">BE=1.0</div><div class="l">保本触发 ATR</div></div>
  <div class="kpi"><div class="v">CD=1</div><div class="l">离场冷却(棒)</div></div>
  <div class="kpi"><div class="v">AT=1.5</div><div class="l">加仓门槛 ATR</div></div>
</div>
<div class="note">门控沿用 V5X：<code>{{A1S, B3, M1a_pb, M5}}</code>。BA=后复权 9 棒/日（2016–2026，56 品种）；THS=后复权 6 棒/日（2020–2026-06，55 品种）。</div>
<table><thead><tr><th>网格</th><th>笔数</th><th>净利(¥)</th><th>PF</th><th>MDD(¥)</th><th>MAR</th><th>train净(¥)</th><th>holdout净(¥)</th></tr></thead>
<tbody>
{mrow('BA','M5','BA（9棒/日）',bold=True)}
{mrow('THS','M5','THS（6棒/日）')}
</tbody></table>
<div class="note"><b>解读：</b>跨网格 edge 稳健——THS 上 PF=1.16&gt;1、holdout 仍为正（¥242K），说明策略信号不依赖某一特定网格。<b>但 THS 量级明显更弱</b>：PF 1.16 vs 1.28、MAR 2.64 vs 8.58。原因：① THS 仅 2020+，且 holdout 只有 2026 上半年；② 6 棒/日较粗，回踩/突破触发更稀疏；③ THS 净利以后复权价计，<b>绝对值不可与 BA 原始价净利直接比较</b>，但 PF/方向性结论可比。建议把 THS 作为「弱一致性佐证」，正式参数仍以 BA 网格为准。</div>
</div>

<h2>三、M5 门控：方向过滤替换比对（请求#3）</h2>
<div class="card">
<h3>现状与替换</h3>
<ul>
<li>当前 V5X 用 <code>M5</code>：仅 <code>|会员净持仓 z| ≥ 1.0</code> 的<b>幅度门</b>（不要求方向一致）。</li>
<li>代码中已存在方向过滤变体 <code>M5b</code>：<code>|z|≥1.0 且 sign(z)==信号方向</code>，但 V5X <b>未启用</b>。你记忆中的「调整成方向过滤」即指此变体。</li>
<li>下面对两网格分别用 M5 vs M5b 同参数回测，看是否应替换。</li>
</ul>
<table><thead><tr><th>网格 / 门控</th><th>笔数</th><th>净利(¥)</th><th>PF</th><th>MDD(¥)</th><th>MAR</th><th>train净(¥)</th><th>holdout净(¥)</th></tr></thead>
<tbody>
{mrow('BA','M5','BA · M5（幅度，现用）',bold=True)}
{mrow('BA','M5b','BA · M5b（方向过滤）')}
{mrow('THS','M5','THS · M5（幅度，现用）',bold=True)}
{mrow('THS','M5b','THS · M5b（方向过滤）')}
</tbody></table>
<div class="note"><b>结论：方向过滤（M5b）不优于幅度门（M5），反而更差——</b>
<ul>
<li>BA：M5b 小幅抬升 PF（1.287）与降低回撤，但 <b>holdout 净利被砍近半</b>（¥458K vs ¥844K）、总净利 −5%。</li>
<li>THS：M5b 在 PF/净利/MAR 上<b>全面下降</b>，且 <b>holdout 转负（−¥97K）</b>。</li>
<li>机制：M5b 是 M5 的子集（更严），训练段更干净但<b>过拟合训练</b>；会员净持仓 z 的方向与未来价格方向一致性弱（且带 available_at 滞后），用作同向过滤会滤掉有效信号、保留噪声。因此 <b>建议保留 M5（幅度门），不替换为方向过滤</b>。</li>
</ul>
</div>
</div>

<h2>四、行动建议</h2>
<div class="card">
<ol>
<li><b>数据层</b>：203 前导 NaN 已修；CJ888 负价系复权损坏，保持 <code>EXCLUDE</code> 或单独重建其 BA。其余表结构/字段未动。</li>
<li><b>参数层</b>：推荐参数（SL3/TR2/BE1/CD1/AT1.5）在 BA 与 THS 两网格上均 PF&gt;1、holdout&gt;0，可锁定为 V5X 正式版。</li>
<li><b>门控层</b>：保留 M5 幅度门，不启用 M5b 方向过滤（OOS 更差）。</li>
<li><b>下一步</b>：① 将推荐参数写回 <code>step46</code> 全局量并锁定；② 2026 全年走完后复核 holdout；③ 2015 无 1m 源仍暂缓。</li>
</ol>
</div>
</div></body></html>'''

open('report_v5x_review.html', 'w', encoding='utf-8').write(HTML)
print('[done] report_v5x_review.html', len(HTML), 'bytes')
