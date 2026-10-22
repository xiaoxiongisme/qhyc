# -*- coding: utf-8 -*-
"""生成「为什么 EB/EG 2026 尤其 9-10 月大波动仍 0 笔 + 如何交易」HTML 报告。"""
import json
import base64

f = json.load(open('step58_funnel.json', encoding='utf-8'))
png = base64.b64encode(open('step58_signals_2026.png', 'rb').read()).decode()
IMG = f'data:image/png;base64,{png}'

MONTHS = ['2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06',
          '2026-07', '2026-08', '2026-09', '2026-10']


def funnel_table(sym, name):
    d = f[sym]
    rows = []
    for mm in MONTHS:
        v = d.get(mm)
        if v is None:  # 该月无任何候选信号（raw=0 不落桶）
            rows.append(f"<tr><td>{mm[5:]}月</td><td class='c'>—</td><td class='c'>0</td>"
                        f"<td class='c'>0</td><td class='c'>0</td><td class='c'>0</td>"
                        f"<td class='c'>0</td><td class='c'>0</td></tr>")
            continue
        rows.append(
            f"<tr><td>{mm[5:]}月</td><td class='c'>{v['hvol']*100:.0f}%</td>"
            f"<td class='c'>{v['raw']}</td><td class='c'>{v['p2']}</td>"
            f"<td class='c'>{v['raw']-v['p2']}</td><td class='c'>{v['m1a']}</td>"
            f"<td class='c'>{v['p2']-v['m1a']}</td><td class='c'>{v['m5']}</td></tr>")
    t = d['_total_2026']
    rows.append(
        f"<tr class='tot'><td>全年</td><td class='c'>{t['hvol']*100:.0f}%</td>"
        f"<td class='c'>{t['raw']}</td><td class='c'>{t['p2']}</td>"
        f"<td class='c'>{t['raw']-t['p2']}</td><td class='c'>{t['m1a']}</td>"
        f"<td class='c'>{t['p2']-t['m1a']}</td><td class='c'>{t['m5']}</td></tr>")
    return f"""
<h3>{name}（{sym}）2026 月度漏斗</h3>
<table><thead><tr>
<th>月份</th><th>高波日占比</th><th>候选(raw)</th><th>过P2</th><th>被P2砍</th>
<th>过M1a</th><th>被M1a砍</th><th>成交</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
"""


html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>为什么 EB/EG 大波动期仍 0 笔 · 如何交易</title>
<style>
:root{{--bg:#0f1419;--card:#1a212b;--ink:#e6edf3;--mut:#8b98a5;--line:#2a3340;
--red:#ff5b5b;--grn:#26a69a;--accent:#4da3ff;--warn:#ffb454;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.65}}
.wrap{{max-width:1040px;margin:0 auto;padding:32px 22px 64px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:19px;margin:32px 0 12px;border-left:4px solid var(--accent);padding-left:10px}}
h3{{font-size:15.5px;margin:20px 0 6px;color:var(--accent)}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0}}
th,td{{border:1px solid var(--line);padding:6px 8px}}
th{{background:#222b36;font-weight:600}} td.c{{text-align:center}}
tbody tr:nth-child(even){{background:#161d26}}
tr.tot td{{background:#23344a;font-weight:600}}
img{{width:100%;border-radius:8px;border:1px solid var(--line);margin:8px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}}
.callout{{border-left:4px solid var(--warn);background:#241c10;padding:12px 16px;border-radius:6px;margin:14px 0}}
.callout.good{{border-color:var(--grn);background:#0f231f}}
.callout b{{color:var(--warn)}} .callout.good b{{color:var(--grn)}}
code{{background:#0f1620;padding:1px 5px;border-radius:4px;color:#9fd0ff;font-size:12.5px}}
ul{{padding-left:20px}} li{{margin:6px 0}}
.kpi{{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0}}
.kpi div{{flex:1;min-width:160px;background:#0f1620;border:1px solid var(--line);border-radius:8px;padding:10px 12px}}
.kpi b{{display:block;font-size:19px;color:var(--accent)}} .kpi span{{font-size:12px;color:var(--mut)}}
.tag{{display:inline-block;background:#23344a;color:#cfe3ff;border-radius:5px;padding:1px 8px;font-size:12px;margin-right:6px}}
</style></head><body><div class="wrap">

<h1>为什么 EB / EG 在 2026（尤其 9–10 月大波动）几乎零交易？</h1>
<div class="sub">融合策略 V5X · 体系门脱敏诊断 · 数据：prod50 主连 60m，<b>覆盖至 2026-10-16</b>（9–10 月确在样本内）· 2026-10-20</div>

<div class="callout">
<b>一句话答案：不是没数据，而是策略「主动不交易大波动」。</b><br>
V5X 的第二道门 <code>P2 高波日禁开仓</code>：当日振幅 &gt; 250 日 80 分位就禁止开仓。2026 年 EB 有 <b>44.9%</b>、EG 有 <b>50.6%</b> 的交易日属高波日（9 月 EG 52%、10 月 56%），大波动期恰好是 P2 全面封锁的时段；剩余在「平波日」出现的候选，又多数被 <code>M1a（MACD 柱同号门）</code>拒掉。于是大波动 → 0 成交，是设计使然，不是漏单。
</div>

<div class="kpi">
<div><b>EB 2026</b><span>候选 23 → 被 P2 砍 11 → 被 M1a 砍 10 → <b style="color:#26a69a">成交 2</b>（仅 2 月、8 月）</span></div>
<div><b>EG 2026</b><span>候选 14 → 被 P2 砍 8 → 被 M1a 砍 6 → <b style="color:#ff5b5b">成交 0</b></span></div>
<div><b>9–10 月</b><span>EG 9 月候选 3（2 被高波封、1 被 MACD 拒）、10 月候选 2（全被高波封）</span></div>
</div>

<h2>一、候选信号落在哪？看图</h2>
<p>下图为 EB/EG 2026 年 60m 收盘价。<b>红色竖带 = 高波日（P2 禁开仓）</b>；▲/▼ = 每个候选信号（绿多/红空）；<b>金色星 = 真正成交</b>。可见：</p>
<ul>
<li>EB 的 3 月急涨、EG 的 9–10 月拉升，都压在密集红带上——大波动日一律不许开仓。</li>
<li>EG 全年的三角候选没有一个变成星：<b>14 个候选全灭</b>，其中 8 个撞高波日、6 个被 MACD 反向拒。</li>
<li>EB 仅 2 个候选（2 月、8 月，均为平波月）成交。</li>
</ul>
<img src="{IMG}" alt="EB/EG 2026 signals vs high-vol days">

<h2>二、逐月漏斗（原始候选 → 过 P2 → 过 M1a → 成交）</h2>
{funnel_table('EB888','苯乙烯')}
{funnel_table('EG888','乙二醇')}
<p class="sub" style="margin-top:-4px">注：「—」表示该月连候选信号都未形成（raw=0，通常是 EMA140 趋势/回踩结构未出现）。「过P2」= 通过高波日过滤的候选数；「被P2砍」= raw − 过P2；「过M1a」= 通过 MACD 柱同号门；「被M1a砍」= 过P2 − 过M1a；「成交」= 最终开仓数。</p>

<h2>三、机制：两道门各砍掉什么</h2>
<div class="card">
<p><b>① P2 高波日禁开仓（砍得最多）。</b> 逻辑：<code>若当日振幅 &gt; 250日80分位 → 当日禁止开新仓</code>。2026 是高波年（EB 44.9% / EG 50.6% 的交易日触发），9–10 月更高（EG 52% / 56%）。所以大波动期本身就被系统判定为「不参与」。</p>
<p><b>② M1a 回踩 MACD 柱同号门（砍掉剩下的）。</b> 逻辑：回踩做多要求 <code>MACD 柱 &gt; 0</code>、做空要求 <code>&lt; 0</code>。EG 在 2026 的 6 个「平波日候选」<b>全部 MACD 柱反向</b>——说明这些回踩发生在动能仍向下/上的反抽里，系统判为假回踩。</p>
<p>两者叠加：EG 14 → 8（P2）→ 0（M1a）；EB 23 → 12（P2）→ 2（M1a）。</p>
</div>

<div class="callout good">
<b>关键区分：EB 与 EG 的「0」性质不同。</b><br>
· <b>EB</b>：2026 并非全年 0（2–8 月有 2 笔），但 <b>9–10 月连候选都没形成</b>（9 月 raw=0；10 月无候选）——价格走势太急、没给出 EMA140 趋势+回踩结构。<br>
· <b>EG</b>：9–10 月<b>有候选</b>（3+2），但被 P2 高波日 + M1a 全部拒掉——是「信号被门控杀死」，不是「没有信号」。<br>
这与上一轮结论一致：EB 属趋势持续型、EG 属偏离回调型；EG 的大波动更接近「急涨急跌后要回归」，本就不是 V5X 的菜。
</div>

<h2>四、那 EG 的大波动到底怎么交易？</h2>
<div class="callout">
<b>先认清一个矛盾：V5X 的 P2 就是为了「避开大波动」，所以它不可能同时「交易大波动」。</b>
你不能指望一个开关既禁止高波日开仓、又在高波日赚钱。要交易 9–10 月这种行情，必须换一套「波动扩张 / 偏离回归」的独立打法，而不是改 V5X 的门。
</div>

<div class="card">
<h3>路线 A：波动扩张突破（vol breakout）——顺势吃急涨急跌</h3>
<ul>
<li>触发：突破前 N 根（如 20 根）高低点，且当日振幅放大（ATR 突破）、成交量放大。</li>
<li>止损：k×ATR（如 1.5–2 ATR），<b>随波动放大而放大</b>；止盈用 ATR 吊灯或前高/前低。</li>
<li>仓位：4 万本金下 EG 单手；波动越大仓位越小（按 ATR 反比缩放），避免被扫。</li>
<li>风险：假突破/来回扫。需加「ADX 上行 + 量能」双确认，宁可少做。</li>
</ul>
<h3>路线 B：偏离回归（emadist，价-EMA140 偏离 z）——吃急涨后的回抽</h3>
<ul>
<li>触发：<code>|价格 − EMA140| / ATR</code> 显著放大（如 ≥2），即价格远离均线；出现反转确认（吞没/长影/动能衰竭）后<b>反向</b>入场。</li>
<li>目标：回归 EMA140 附近；止损放在极端点外侧。</li>
<li>适配：这正是 EG「偏离回调型」体质的顺风局，前轮研究已提示 EG→emadist。</li>
<li>风险：趋势行情中「偏离还能更偏离」，必须带硬止损、不扛单。</li>
</ul>
<h3>路线 C：放宽 P2 + 波动自适应离场（改造 V5X，最不推荐）</h3>
<ul>
<li>保留趋势回踩入场，但允许高波日开仓，并把固定 2ATR 止损换成波动自适应止损、仓位缩小。</li>
<li>问题：V5X 的 2ATR 固定止损是为平波设计的，急涨急跌里会被瞬间击穿；等于把一套调好的系统拆了重调，过拟合风险高。</li>
</ul>
</div>

<div class="callout">
<b>给你的实操建议（4 万本金、专注 EB/EG）：</b><br>
1) <b>若坚持 V5X 原教旨</b>：9–10 月就空仓等待——P2 说别做，就别做，这本身就是风控。等回到平波日的回踩信号再进（如 EB 2 月/8 月那两笔）。<br>
2) <b>若想抓这波大波动</b>：另起一个「路线 A 突破」或「路线 B 偏离回归」的小模块，<b>单笔只用 1 手、止损按 ATR 放大</b>，且与 V5X 分开记账、分开风控，不要混在一个仓位里。<br>
3) <b>不要用「放宽 P2」去硬掰 V5X</b>——那会把一套有正期望的系统改成没验证过的系统。
</div>

<h2>五、要不要我把路线 A/B 做成可回测模块？</h2>
<p class="sub">如果你确认想交易 EG 的大波动，我可以直接用现有 prod50 数据，把「波动扩张突破」与「偏离回归(emadist)」各写成一版可回测引擎，在 EG 2026 9–10 月与全样本上跑给你看胜率/PF/回撤，再决定要不要上。V5X 继续只做平波趋势，两套并行、分开风控。</p>

</div></body></html>"""

open('step58_why_zero.html', 'w', encoding='utf-8').write(html)
print('saved step58_why_zero.html', len(html), 'bytes')
