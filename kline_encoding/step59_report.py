# -*- coding: utf-8 -*-
"""step59 —— 生成 A/B 两版「抓大波动」引擎的对比报告 ab_report.html"""
import json
import pandas as pd

R = json.load(open('step59_results.json', encoding='utf-8'))
D = json.load(open('step59b_drilldown.json', encoding='utf-8'))


def fnum(x, d=0):
    if x is None or (isinstance(x, float) and x != x):
        return '—'
    return f'{x:,.{d}f}'


def pct(x):
    return '—' if x is None else f'{x*100:.1f}%'


def verdict_row(kind, tag, m):
    ho = m.get('holdout', {})
    cls = 'pos' if m.get('net_yuan', 0) > 0 else 'neg'
    return (f"<tr><td>{kind}</td><td class='mono'>{tag}</td>"
            f"<td class='c'>{m.get('n','—')}</td>"
            f"<td class='c'>{pct(m.get('win'))}</td>"
            f"<td class='c'>{fnum(m.get('pf'),2)}</td>"
            f"<td class='c {cls}'>{fnum(m.get('net_yuan'))}</td>"
            f"<td class='c'>{fnum(m.get('mdd_yuan'))}</td>"
            f"<td class='c {cls}'>{fnum(ho.get('net_yuan'))}</td></tr>")


def grid_table(kind):
    rows = []
    for tag, m in R[kind].items():
        p = tag.split(':', 1)[1]
        rows.append(verdict_row(kind, p, m))
    return ''.join(rows)


def sym_block(sym, name):
    b = D['B'][sym]['m']
    a = D['A'][sym]['m']
    return f"""
<div class="card">
  <h3>{name} <span class="sym">{sym}</span></h3>
  <table class="mini">
    <thead><tr><th>引擎</th><th>笔数</th><th>胜率</th><th>PF</th><th>净利(元)</th>
      <th>2026净利</th><th>2026笔数</th><th>2026胜率</th></tr></thead>
    <tbody>
      <tr><td>Route A 突破</td><td class='c'>{a.get('n','—')}</td><td class='c'>{pct(a.get('win'))}</td>
        <td class='c'>{fnum(a.get('pf'),2)}</td><td class='c {'pos' if a.get('net_yuan',0)>0 else 'neg'}'>{fnum(a.get('net_yuan'))}</td>
        <td class='c {'pos' if a.get('holdout',{}).get('net_yuan',0)>0 else 'neg'}'>{fnum(a.get('holdout',{}).get('net_yuan'))}</td>
        <td class='c'>{a.get('holdout',{}).get('n','—')}</td><td class='c'>{pct(a.get('holdout',{}).get('win'))}</td></tr>
      <tr><td>Route B 回归</td><td class='c'>{b.get('n','—')}</td><td class='c'>{pct(b.get('win'))}</td>
        <td class='c'>{fnum(b.get('pf'),2)}</td><td class='c {'pos' if b.get('net_yuan',0)>0 else 'neg'}'>{fnum(b.get('net_yuan'))}</td>
        <td class='c {'pos' if b.get('holdout',{}).get('net_yuan',0)>0 else 'neg'}'>{fnum(b.get('holdout',{}).get('net_yuan'))}</td>
        <td class='c'>{b.get('holdout',{}).get('n','—')}</td><td class='c'>{pct(b.get('holdout',{}).get('win'))}</td></tr>
    </tbody>
  </table>
</div>"""


def monthly_table(sym):
    rows = []
    for r in D['B'][sym]['monthly']:
        cls = 'pos' if r['net'] > 0 else 'neg'
        rows.append(f"<tr><td>{r['ym']}</td><td class='c'>{r['n']}</td>"
                    f"<td class='c {cls}'>{fnum(r['net'])}</td>"
                    f"<td class='c'>{pct(r['win'])}</td></tr>")
    return ''.join(rows)


HTML = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Route A / B 抓大波动引擎 · 回测报告</title>
<style>
:root {{ color-scheme: dark; }}
body {{ background:#0d1117; color:#e6edf3; font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       margin:0; padding:32px 40px; line-height:1.65; }}
h1 {{ font-size:26px; margin:0 0 6px; }}
h2 {{ font-size:20px; margin:34px 0 10px; border-left:4px solid #58a6ff; padding-left:10px; }}
h3 {{ font-size:16px; margin:6px 0 10px; }}
.sub {{ color:#8b98a5; font-size:13px; margin:0 0 8px; }}
.sym {{ color:#58a6ff; font-family:monospace; font-size:13px; }}
.card {{ background:#161b22; border:1px solid #21262d; border-radius:10px; padding:16px 18px; margin:12px 0; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin:8px 0; }}
th,td {{ padding:7px 9px; border-bottom:1px solid #21262d; text-align:left; }}
th {{ color:#8b98a5; font-weight:600; font-size:12px; }}
.c {{ text-align:center; }}
.mono {{ font-family:monospace; font-size:12px; }}
.pos {{ color:#3fb950; }} .neg {{ color:#ff5b5b; }}
.mini td:first-child {{ color:#c9d1d9; font-weight:600; }}
.badge {{ display:inline-block; padding:2px 9px; border-radius:20px; font-size:12px; font-weight:600; }}
.bad {{ background:#3d1a1a; color:#ff5b5b; }} .good {{ background:#1a3d24; color:#3fb950; }} .warn {{ background:#3d341a; color:#d4a72c; }}
.quote {{ background:#161b22; border-left:3px solid #d4a72c; padding:12px 16px; margin:14px 0; color:#c9d1d9; font-size:14px; }}
code {{ background:#21262d; padding:1px 6px; border-radius:4px; font-size:12px; color:#e6edf3; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:900px) {{ .grid2 {{ grid-template-columns:1fr; }} body {{ padding:20px; }} }}
</style></head><body>

<h1>两版「抓大波动」引擎 · A / B 回测</h1>
<p class="sub">目标：EG 9–10 月大波动 V5X 因 P2 高波日禁开仓而空仓，另起两套独立引擎尝试吃这段行情。
同一成本模型 / 预热 / EOD / OOS(2026) 口径。数据截至 2026-10-16。</p>

<div class="quote">
<b>一句话结论：</b>A（波动扩张突破）与 B（偏离回归）在 EG/EB 上<b>都不赚钱</b>——A 被 60m 化工的反复假突破来回打脸，B（逆势摸偏离）在大波动趋势里被一路碾压。
这恰好反证了 V5X 的 P2「高波日禁开仓」不是 bug，而是<b>有效的风控</b>：9–10 月这种行情对朴素系统是绞肉机。
</div>

<h2>1. 两版引擎定义</h2>
<div class="grid2">
<div class="card">
<h3>Route A｜波动扩张突破</h3>
<p class="sub">入场：当根 <code>ATR/近20根ATR均值 ≥ vol_ratio</code>（波动扩张）
且 <code>收盘 &gt; 前N根最高</code>（或 &lt; 最低）且 <code>c &gt; o</code>。<br>
方向：随突破方向（不叠加 EMA140 方向门，避免与突破冲突）。<br>
离场：2ATR 初始止损 + 2ATR 吊灯 + 0.5ATR 保本。</p>
</div>
<div class="card">
<h3>Route B｜偏离回归 (emadist)</h3>
<p class="sub">入场：<code>|收盘 − EMA140| / ATR ≥ dev_thr</code>（大幅偏离均值）。
偏离在上 → 做空回归；偏离在下 → 做多回归。<br>
方向：逆偏离（摸均值回归）。<br>
离场：价格回到 EMA140（止盈）+ 2ATR 硬止损 + 0.5ATR 保本。</p>
</div>
</div>

<h2>2. 全参数网格结果（EG+EB+MA 合并）</h2>
<table>
<thead><tr><th>引擎</th><th>参数</th><th>笔数</th><th>胜率</th><th>PF</th>
<th>净利(元)</th><th>最大回撤</th><th>2026净利</th></tr></thead>
<tbody>
{grid_table('A')}
{grid_table('B')}
</tbody></table>
<p class="sub">注：合并口径下 B 的 2026 正值主要来自 MA（见下节逐品种）；EG/EB 均亏损。</p>

<h2>3. 逐品种拆解（关键配置）</h2>
{sym_block('EG888', '乙二醇')}
{sym_block('EB888', '苯乙烯')}
{sym_block('MA888', '甲醇（对照）')}

<h2>4. EG 逐月明细（Route B, DEV=2.5）</h2>
<div class="card">
<table class="mini">
<thead><tr><th>月份</th><th>笔数</th><th>净利(元)</th><th>胜率</th></tr></thead>
<tbody>{monthly_table('EG888')}</tbody></table>
<p class="sub">9 月（大波动）净利 +3,084、10 月 −1,388；但 3 月 −12,172、7 月 −5,142、5 月 −3,032 把全年拖成 −5,642。
即：<b>大波动月并未稳定贡献，亏损主要来自平波月的逆势摸偏离。</b></p>
</div>

<h2>5. 为什么两版都失败（机制解释）</h2>
<div class="card">
<p>① <b>化工 60m 是「趋势中夹噪音」结构</b>：突破（A）在趋势延续前先被回抽触发 2ATR 止损，反复磨损；
试算 A 在 EG 上 2026 仅 7 笔就 −10,592，样本极少但方向明确（OOS 亏）。</p>
<p>② <b>大波动 = 高 ADX = 强趋势，逆势摸偏离（B）必然被碾压</b>：价格偏离 2.5ATR 后还能再走 5–6ATR，
2.5ATR 的止损太窄、回归目标（EMA140）太远，胜率仅 26–28%，成本一吃就转负（EG PF 0.83 / EB 0.69）。</p>
<p>③ <b>MA 是唯一在 B 上 2026 转正的品种</b>（+7,798 / PF1.29），因为甲醇的均值回归性更强；
但这只说明「哪个品种适合回归」，不说明「大波动可稳定套利」。</p>
</div>

<h2>6. 那 EG 的大波动到底怎么参与？</h2>
<div class="card">
<p><b>诚实答案：不建议用 A/B 这类 60m 朴素系统去硬吃。</b> 证据在表里——两版都亏，且亏损集中在趋势段。
可行的方向只有两条，且都需要额外验证：</p>
<p><b>方向一（推荐）：接受 V5X 的设计。</b> P2 高波日空仓不是缺陷，是它避开绞肉机的核心。等平波日的 EMA140 回踩再入，
这正是 V5X 在 EG 上 38–41% 胜率、PF 2.6–3.3 的来源。</p>
<p><b>方向二：若坚持要波动暴露，做成「体制自适应」而非「摸偏离」。</b>
即：高波日只做<b>同向趋势跟随</b>、止损放宽到 3–4ATR、并且<b>降低单笔仓位</b>（因为高波日 ATR 大），
再用「波动收缩后离场」而非固定目标。这是一套独立的小系统，需单独 OOS 验证，不能与 V5X 混仓。</p>
</div>

<h2>7. 纪律与局限</h2>
<div class="card">
<p>① <b>EG/EB 的 A 样本极少</b>（EG 37 笔、EB 25 笔），结论方向可信但幅度置信宽；B 样本充分（EG 715 / EB 684），结论稳健。</p>
<p>② <b>本报告已修复一个隐蔽 bug</b>：EG/EB 60m 序列起始有 6/9 根全 NaN 棒，手写递推 ATR 遇 NaN 会<b>永久中毒</b>（首 14 根均值含 NaN → 全序列 NaN → 0 信号）。
已用「丢弃 NaN 棒」修复，修复前 A/B 在 EG/EB 上均 0 笔（假象）。此为项目记忆里「NaN 僵尸仓」同源问题。</p>
<p>③ 参数为小网格扫描，存在隐式过拟合；靠逐品种 + OOS(2026) 双向看，未做单点最优挑选。</p>
<p>④ 上线前仍需：EG/EB 双品种并发风控叠加对拍、真实滑点压力测试。</p>
</div>

<p class="sub" style="margin-top:24px">生成于 2026-10-20 · 数据 prod50（56 品种 60m，截至 2026-10-16）· 脚本 step59_ab_engines.py / step59b_drilldown.py</p>
</body></html>"""

open('ab_report.html', 'w', encoding='utf-8').write(HTML)
print('saved ab_report.html', len(HTML), 'bytes')
