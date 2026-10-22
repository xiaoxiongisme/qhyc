# -*- coding: utf-8 -*-
"""生成 dirpersist 替代会员数据 的 EB/EG 回测报告 HTML。"""
import json

r = json.load(open('step57_results.json', encoding='utf-8'))
wm = json.load(open('step57b_wholemarket.json', encoding='utf-8'))

NS = [1, 2, 3, 5, 8, 10, 15, 20]


def fmt(x, d=0):
    if x is None:
        return '-'
    try:
        if isinstance(x, float) and x != x:
            return 'NA'
    except Exception:
        pass
    return f'{x:,.{d}f}'


def pfstr(m):
    pf = m['pf']
    if pf is None or (isinstance(pf, float) and pf != pf):
        return 'NA'
    return f'{pf:.2f}'


def row_eb_eg(sym, key, label, N=None):
    d = r[sym][key]
    m, is_, ho = d['m'], d['is_'], d['ho']
    n = d.get('N')
    nn = '-' if n is None else str(int(n))
    win = '-' if m['win'] != m['win'] else f"{m['win']*100:.1f}"
    iswin = '-' if is_['win'] != is_['win'] else f"{is_['win']*100:.1f}"
    howin = '-' if ho['win'] != ho['win'] else f"{ho['win']*100:.1f}"
    return (f"<tr><td>{label}</td><td class='c'>{nn}</td><td class='c'>{m['n']}</td>"
            f"<td class='r'>{fmt(m['net'])}</td><td class='c'>{pfstr(m)}</td><td class='c'>{win}</td>"
            f"<td class='c'>{is_['n']}</td><td class='r'>{fmt(is_['net'])}</td><td class='c'>{iswin}</td>"
            f"<td class='c'>{ho['n']}</td><td class='r'>{fmt(ho['net'])}</td><td class='c'>{howin}</td>"
            f"<td class='c'>{d['y25']}</td><td class='c'>{d['y26']}</td></tr>")


def table_for(sym, name):
    rows = []
    rows.append(row_eb_eg(sym, 'M5_member', 'M5 会员(基准)', None))
    rows.append(row_eb_eg(sym, 'none', 'none 去体制门', None))
    for N in NS:
        rows.append(row_eb_eg(sym, f'dirpersist_{N}', 'dirpersist 同向N根', N))
    return f"""
<h3>{name}（{sym}）</h3>
<table>
<thead><tr>
<th rowspan="2">体制门配置</th><th rowspan="2">N</th>
<th colspan="4">全样本(2018/19–2026)</th>
<th colspan="4">样本内 IS(≤2025)</th>
<th colspan="4">样本外 HO(≥2026)</th>
<th colspan="2">关键年份笔数</th>
</tr><tr>
<th>笔</th><th>净利</th><th>PF</th><th>胜率%</th>
<th>笔</th><th>净利</th><th>胜率%</th>
<th>笔</th><th>净利</th><th>胜率%</th>
<th>2025</th><th>2026</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<p class="note">净利单位：元。PF=盈利总额/亏损总额；胜率=盈利笔/总笔。dirpersist 的 N 即「价格连续 N 根维持 EMA140 同向」的门限。</p>
"""


wm_rows = []
for cname, label in [('M5_member', 'M5 会员(基准)'), ('dirpersist_5', 'dirpersist N=5'),
                     ('dirpersist_10', 'dirpersist N=10')]:
    m = wm[cname]
    wm_rows.append(
        f"<tr><td>{label}</td><td class='c'>{m['n']:,}</td><td class='r'>{fmt(m['net'])}</td>"
        f"<td class='c'>{pfstr(m)}</td><td class='c'>{m['win']*100:.1f}</td>"
        f"<td class='c'>{m['ho_n']}</td><td class='r'>{fmt(m['ho_net'])}</td></tr>")

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dirpersist 替代会员数据 · EB/EG 回测</title>
<style>
:root{{--bg:#0f1419;--card:#1a212b;--ink:#e6edf3;--mut:#8b98a5;--line:#2a3340;
--up:#ff5b5b;--dn:#26a69a;--accent:#4da3ff;--warn:#ffb454;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.6}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 22px 64px}}
h1{{font-size:25px;margin:0 0 4px}}
h2{{font-size:19px;margin:34px 0 12px;border-left:4px solid var(--accent);padding-left:10px}}
h3{{font-size:16px;margin:22px 0 8px;color:var(--accent)}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}}
th,td{{border:1px solid var(--line);padding:6px 7px;text-align:left}}
th{{background:#222b36;color:var(--ink);font-weight:600}}
td.c{{text-align:center}} td.r{{text-align:right;font-variant-numeric:tabular-nums}}
tbody tr:nth-child(even){{background:#161d26}}
tr.hl td{{background:#23344a}}
.note{{color:var(--mut);font-size:11.5px;margin:4px 0 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}}
.kpi{{display:flex;flex-wrap:wrap;gap:14px;margin:10px 0}}
.kpi div{{flex:1;min-width:150px;background:#0f1620;border:1px solid var(--line);border-radius:8px;padding:10px 12px}}
.kpi b{{display:block;font-size:20px;color:var(--accent)}}
.kpi span{{font-size:12px;color:var(--mut)}}
.callout{{border-left:4px solid var(--warn);background:#241c10;padding:12px 16px;border-radius:6px;margin:16px 0}}
.callout.good{{border-color:var(--dn);background:#0f231f}}
.callout b{{color:var(--warn)}} .callout.good b{{color:var(--dn)}}
code{{background:#0f1620;padding:1px 5px;border-radius:4px;color:#9fd0ff;font-size:12.5px}}
ul{{margin:8px 0 8px 2px;padding-left:20px}} li{{margin:5px 0}}
.tag{{display:inline-block;background:#23344a;color:#cfe3ff;border-radius:5px;padding:1px 8px;font-size:12px;margin-right:6px}}
</style></head><body><div class="wrap">

<h1>用「EMA140 同向持续性」替代会员数据 —— EB / EG 重回测</h1>
<div class="sub">融合策略 V5X · 体制门替代实验 · 数据：prod50（EB 2019–2026 / EG 2018–2026，60m 主连）· 生成 2026-10-20</div>

<div class="card">
<h2 style="margin-top:4px">一、替代机制是什么</h2>
<p>原 V5X 第五道门控 <code>M5</code> 依赖<b>跨品种会员净持仓 z 分数</b>（|z|≥1.0 才放行），这是机构持仓的「体制」信号，但单品种、无会员数据、或会员数据在某品种上尺度失真时（如 EB/EG 2025–2026）会整体失效。</p>
<p>本次用纯单品种可计算量替代：<b>「价格连续 N 根维持 EMA140 同向」</b>。引擎内部用 <code>dir_run</code> 统计「收盘位于 EMA140 同侧」的连续根数，门控要求 <code>dir_run ≥ N</code>。即：只在趋势已持续运行 N 根后才允许顺势开仓——本质是<b>趋势体制的单品种代理</b>，完全不触碰会员数据。</p>
<p><span class="tag">ALT_GATE='dirpersist'</span><span class="tag">ALT_THR = N</span><span class="tag">零前视（取前一根方向）</span></p>
</div>

<div class="card">
<h2 style="margin-top:4px">二、EB / EG 逐 N 回测（含样本内/外与关键年份）</h2>
{table_for('EB888','苯乙烯')}
{table_for('EG888','乙二醇')}
</div>

<div class="callout good">
<b>核心结论 1 —— EB 的「2025–2026 零信号」是会员数据假象，dirpersist 已修复。</b><br>
M5 会员门在 EB 上 2025、2026 各 0 笔；但 <code>none</code>（去门）显示 EB 这两年本有 2+2 笔信号——它们全被会员 |z| 过小拒绝。dirpersist 在 N=3~8 时稳定补回：<b>N=5 → 1 笔(2025)+2 笔(2026)，N=8 → 1+1</b>。趋势持续性代理成功替代了缺失的会员体制信号。
</div>

<div class="callout">
<b>核心结论 2 —— EG 的 2026 零信号是「真缺席」，与门控无关，dirpersist 救不回。</b><br>
EG 在 <code>none</code>（连体制门都去掉）下 2026 仍是 <b>0 笔</b>，dirpersist 各 N 同样 0 笔。说明 2026 年 EG 根本没有通过前四道门（高波日/回踩MACD/ADX/尾盘）的顺势 setup——这是品种自身的<b>震荡/偏离回归体制</b>（EG 属「偏离回调型」），不是会员数据缺失。换言之：若把 EG 选作主打品种，须接受 2026 这类年份可能整年无信号。
</div>

<div class="card">
<h2 style="margin-top:4px">三、推荐 N（按品种体质分设）</h2>
<div class="kpi">
<div><b>EB · N≈5–8</b><span>趋势持续型。N=5：18笔/PF1.99/1(25)+2(26)；N=8：15笔/PF2.42/1+1。取 N=5 样本更足、2026 仍存活；偏好高 PF 取 N=8。</span></div>
<div><b>EG · N≈3</b><span>偏离回归型，高 N 反而劣化（N=15 转负）。N=3：24笔/PF2.60/胜率41.7%，略优于原 M5(2.59)。但 2026 恒为 0。</span></div>
</div>
<p class="note">注意：EB 偏好「较大 N」（趋势需成熟），EG 偏好「较小 N」（回调即入），二者体质相反——这正是同一机制需按品种校准 N 的原因。若强行统一，N=5 是折中（EB 尚可、EG 仍正）。</p>
</div>

<div class="card">
<h2 style="margin-top:4px">四、全市场确认：dirpersist 不是退化替代</h2>
<table><thead><tr><th>体制门</th><th>总笔</th><th>总净利</th><th>PF</th><th>胜率%</th><th>HO笔</th><th>HO净利(2026)</th></tr></thead>
<tbody>{''.join(wm_rows)}</tbody></table>
<p class="note">56 品种全样本。dirpersist N=5：总净利 <b>206.3万 &gt; M5 的 204.2万</b>，且样本外 2026 更优（76.9万 vs 73.0万）；N=10 略低于 M5 但同量级。结论：单品种趋势持续性代理在全局口径下可平替甚至小幅超越会员数据体制门。</p>
</div>

<div class="card">
<h2 style="margin-top:4px">五、上线前须知（纪律）</h2>
<ul>
<li><b>过拟合风险：</b>N 为全样本扫描选出的甜点，靠全市场 OOS（2026）间接验证；单品种 EB/EG 样本仅 10–26 笔，置信区间极宽，勿过度解读某一 N 的 PF。</li>
<li><b>EB/EG 信号稀疏：</b>单一品种年均仅 2–3 笔（60m 趋势过滤的固有低频）。4 万本金、单手（风险≈605元/手≈1.5%）完全匹配；加仓≤2 手（≈3%）仍可。</li>
<li><b>EG 2026 缺席：</b>若选 EG，需接受「某些年份零交易」的体制风险；可考虑 EG 改用 <code>emadist</code>（价-EMA140 偏离z）门，更贴合其回归体质（前轮研究已提示 EG→emadist）。</li>
<li><b>未做并发对拍：</b>EB+EG 双品种同屏持仓的风控叠加（同屏最大暴露、冷却）需用生产组合引擎实跑一遍再上线。</li>
</ul>
</div>

</div></body></html>"""

open('dirpersist_report.html', 'w', encoding='utf-8').write(html)
print('saved dirpersist_report.html', len(html), 'bytes')
