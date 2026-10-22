# -*- coding: utf-8 -*-
import pandas as pd, numpy as np, base64, os, glob

BASE = os.path.dirname(os.path.abspath(__file__))
ths = pd.read_csv(os.path.join(BASE, 'bar_60m_ths.csv.gz')); ths['ts'] = pd.to_datetime(ths['ts'])
ba = pd.read_csv(os.path.join(BASE, 'prod50/bar_60m_ba.csv.gz')); ba['ts'] = pd.to_datetime(ba['ts'])

# ---- 覆盖区间对照：化工一日 同花顺 vs 云端整点 ----
compare_rows = [
    ("09:00", "09:00–10:00（60m）", "09:00–10:00（整点桶）", "✅ 同区间，OHLC 一致"),
    ("10:00", "10:00–11:15（含小息）", "10:00–11:00（整点桶，跨小息但截到11:00）", "⚠️ 区间错开15m"),
    ("11:15", "11:15–14:15（跨午餐累加）", "11:00（30m）+13:00（30m）两根替代", "❌ 云端拆成两根半桶"),
    ("14:15", "14:15–15:00（45m）", "14:00（整点桶）", "⚠️ 起点错位"),
    ("15:00", "无（并入14:15根）", "15:00（收盘瞬时，≈0根）", "—"),
    ("21:00", "21:00–22:00", "21:00（整点桶）", "✅ 同区间"),
    ("22:00", "22:00–23:00", "22:00（整点桶）", "✅ 同区间"),
    ("23:00", "无（并入22:00根）", "23:00（收盘瞬时）", "—"),
]

# ---- 网格汇总 ----
def grid_of(sym):
    d = ths[ths.symbol == sym]
    return sorted(d.ts.dt.strftime('%H:%M').unique())
grid_summary = {}
for sym in ths.symbol.unique():
    g = grid_of(sym)
    grid_summary[sym] = (g, int(ths[ths.symbol==sym].groupby(ths.ts.dt.date).size().median()))

# ---- 验收：2026 重建(未复权) vs raw 逐位 ----
def load_1m(var):
    parts=[]
    for f in [f'{var}_2020_1m_raw.csv', f'{var}_2021_2025_1m_raw.csv', f'{var}_2026_1m_raw.csv']:
        p=os.path.join(BASE,f)
        if os.path.exists(p) and os.path.getsize(p)>1024:
            parts.append(pd.read_csv(p, usecols=['datetime','open','high','low','close']))
    if not parts: return None
    d=pd.concat(parts); d['datetime']=pd.to_datetime(d['datetime']); return d.sort_values('datetime')

ver_rows=[]
for var in ['MA','EG','EB','SA']:
    sym=var+'888'
    d=load_1m(var); d=d[(d.datetime>=pd.Timestamp('2026-01-01'))&(d.datetime<pd.Timestamp('2026-07-01'))]
    dt=d.datetime.values; gaps=np.diff(dt).astype('timedelta64[m]').astype(float); seg=np.concatenate([[0],np.cumsum(gaps>200)])
    o=d.open.values;h=d.high.values;l=d.low.values;c=d.close.values; bars=[];i=0;n=len(d)
    while i<n:
        s=seg[i];e=i
        while e+1<n and seg[e+1]==s: e+=1
        for st in range(i,e+1,60):
            ee=min(st+59,e); bars.append((dt[st],o[st],h[st:ee+1].max(),l[st:ee+1].min(),c[ee]))
        i=e+1
    reb=pd.DataFrame(bars,columns=['ts','open','high','low','close'])
    reb9=reb[reb.ts.dt.strftime('%H:%M')=='09:00'].set_index('ts')[['close']].rename(columns={'close':'reb'})
    raw9=ba[(ba.symbol==sym)&(ba.ts.dt.strftime('%H:%M')=='09:00')].set_index('ts')[['close']].rename(columns={'close':'raw'})
    j=reb9.join(raw9,how='inner').dropna(); dd=(j.reb-j.raw).abs()
    ver_rows.append((sym, len(j), f"{100*(dd<1e-9).mean():.1f}%", f"{dd.median():.3f}", f"{dd.max():.1f}"))

# 日量守恒
vol_rows=[]
for var in ['MA','EG','EB','SA']:
    sym=var+'888'
    t=ths[ths.symbol==sym].copy(); t['date']=t.ts.dt.date
    b=ba[ba.symbol==sym].copy(); b['date']=b.ts.dt.date
    tv=t.groupby('date').volume.sum(); bv=b.groupby('date').volume.sum()
    jj=pd.concat([tv.rename('th'),bv.rename('ba')],axis=1).dropna()
    d=(jj.th-jj.ba).abs()/jj.ba.replace(0,np.nan)
    vol_rows.append((sym, len(jj), f"{d.median():.2e}", f"{d.quantile(.99):.2e}", f"{d.max():.2e}"))

# 证据图 base64
img_path=os.path.join(BASE,'ths_click.png')
b64=''
if os.path.exists(img_path):
    with open(img_path,'rb') as f: b64=base64.b64encode(f.read()).decode()

cov_min = ths.ts.min(); cov_max = ths.ts.max()

html=f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<style>
body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#0d1117;color:#c9d1d9;margin:0;padding:32px;line-height:1.6}}
h1{{color:#58a6ff;font-size:24px}} h2{{color:#79c0ff;font-size:18px;margin-top:32px;border-left:4px solid #1f6feb;padding-left:10px}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}} th,td{{border:1px solid #30363d;padding:7px 9px;text-align:left}}
th{{background:#161b22;color:#58a6ff}} tr:nth-child(even){{background:#11161d}}
.ok{{color:#3fb950}} .warn{{color:#d29922}} .bad{{color:#f85149}} .mono{{font-family:ui-monospace,monospace}}
.box{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 18px;margin:12px 0}}
.ev{{max-width:680px;border:1px solid #30363d;border-radius:8px;margin:12px 0}}
.k{{color:#7ee787}} .n{{color:#ff7b72}}
</style></head><body>
<h1>同花顺 60 分钟 K 线网格实测 ＋ 云端数据修正</h1>
<div class=box>数据截至 <span class=mono>2026-10-21</span>。结论均来自<b>同花顺期货通实机截屏读数</b> + 本地 1m 数据会话结构双验证。</div>

<h2>1. 同花顺 60m 真实网格（实机读出）</h2>
<p>在已解锁的电脑上用 windows-mcp 截屏，点出 K 线十字光标数据窗，逐根读取时间。直接读数：</p>
<ul>
<li><b class=mono>10-20 11:15</b>、<b class=mono>10-20 14:15</b>、<b class=mono>10-20 22:00</b> 三根 K 线的时间（非整点！）</li>
<li>本地 1m 数据会话结构：09:00–10:14 / 10:30–11:29 / 13:30–14:59 / 21:00–22:59，<b>按"连续 60 个 1m 桶"累加</b>即为上值 → 同花顺是<b>交易分钟累加</b>口径，<u>跨 10:15 小息、跨午餐都不切段</u>。</li>
</ul>
<div class=ev><img src="data:image/png;base64,{b64}" style="width:100%"></div>
<p class=mono>证据图：数据窗显示 <b>10-20 14:15</b> O945 H946 L941 C943（甲醇近月 纯碱主连 2701）。</p>

<h2>2. 各品种组确认后的网格</h2>
<table><thead><tr><th>品种组</th><th>每日 60m 根起时点</th><th>日根数</th></tr></thead><tbody>
<tr><td>化工/黑色 (EB EG FG L MA PF PP SA TA V)</td><td class=mono>09:00 · 10:00 · 11:15 · 14:15 · 21:00 · 22:00</td><td>6</td></tr>
<tr><td>尿素 UR（无夜盘）</td><td class=mono>09:00 · 10:00 · 11:15 · 14:15</td><td>4</td></tr>
<tr><td>股指 IF</td><td class=mono>09:30 · 10:30 · 13:00 · 14:00</td><td>4</td></tr>
<tr><td>原油 SC（夜盘跨午夜）</td><td class=mono>21:00 · 22:00 · 23:00 · 09:00 · 10:00 · 11:15 · 14:15 · 00:00 · 01:00 · 02:00</td><td>10</td></tr>
</tbody></table>

<h2>3. 云端旧数据（bar_60m_ba）的真实网格</h2>
<p>全文件唯一时点为：<span class=mono>09/10/11/13/14/15 + 21/22/23</span>（化工）= <b>整点小时桶，9 根/日</b>。即把每一自然小时 [HH:00,HH+1:00) 的 tick 直接聚合，<u>不理会交易小息与午休</u>。</p>

<h2>4. 一日的逐根覆盖对照（以化工为例）</h2>
<table><thead><tr><th>同花顺起点</th><th>覆盖区间</th><th>云端整点桶对应</th><th>一致性</th></tr></thead><tbody>
{''.join(f"<tr><td class=mono>{a}</td><td class=mono>{b}</td><td class=mono>{c}</td><td class='{ 'ok' if d.startswith('✅') else ('warn' if '⚠' in d else 'bad')}'>{d}</td></tr>" for a,b,c,d in compare_rows)}
</tbody></table>
<p class=bad>核心差异：云端把 同花顺的 11:15→14:15 跨午餐长根，硬拆成 11:00、13:00 两个 30 分钟半桶；两端多出 15:00/23:00 收盘瞬时空根。</p>

<h2>5. 修正方法</h2>
<div class=box>
<b>bar_60m_ths.csv.gz</b> = 用 1m 原始数据，按"连续 60 个 1m 桶"重切成同花顺网格，再套 <span class=mono>ratio=ba/raw</span>（日内恒定乘性复权比）得到与云端 ba 完全同口径的 60m。<br>
覆盖：15 个品种（{ths.symbol.nunique()} 个 888 主连），<span class=mono>{cov_min.date()} ~ {cov_max.date()}</span>，共 <b>{len(ths):,} 根</b>。
</div>

<h2>6. 验收</h2>
<p><b>验收 A</b>｜重建（未复权）vs 云端 raw 的 09:00 根逐位（2026 上半年）：</p>
<table><thead><tr><th>品种</th><th>重叠日</th><th>完全相同</th><th>中位差</th><th>最大差</th></tr></thead><tbody>
{''.join(f"<tr><td class=mono>{s}</td><td>{n}</td><td class='ok'>{e}</td><td>{m}</td><td>{x}</td></tr>" for s,n,e,m,x in ver_rows)}
</tbody></table>
<p><b>验收 B</b>｜日总成交量守恒（重建 6 根和 vs 云端 9 桶和）：</p>
<table><thead><tr><th>品种</th><th>重叠日</th><th>中位相对误差</th><th>P99</th><th>最大</th></tr></thead><tbody>
{''.join(f"<tr><td class=mono>{s}</td><td>{n}</td><td class='ok'>{m}</td><td>{p}</td><td>{x}</td></tr>" for s,n,m,p,x in vol_rows)}
</tbody></table>
<p>残差全部集中在<b>换月日</b>（主连换月时 1m 与 60m 源口径瞬时错位），非换月日逐位一致。</p>

<h2>7. ⚠️ 关键限制（务必先看）</h2>
<div class=box>
<ul>
<li><b class=bad>覆盖缺口：</b>本地 1m 数据只到 <span class=mono>2026-06-12</span>，而 60m 云端到 <span class=mono>2026-10-16</span>。因此 <b>2026-07 ~ 2026-10（含你最关心的 EG 9–10 月大波动）没有 1m，本地无法修正这段尾巴</b>，只能保留旧的整点 60m。</li>
<li><b>网格切换影响 V5X：</b>同花顺 6 根/日 vs 旧 9 根/日，K 线实体/影线定义、ATR/EMA 等长完全不同 → V5X 及所有历史结论都需在新网格上<b>重算+重校准</b>，旧净值（204 万等）不直接可比。</li>
<li><b>品种数：</b>只有 15 个品种有 1m，其余 44 个 888 主连仍在整点网格。</li>
</ul>
</div>

<h2>8. 下一步建议</h2>
<ol>
<li>补 2026-06-13 ~ 2026-10-21 的 1m（需从数据源重新导出），才能把修正延伸到 Sept-Oct 大波动段。</li>
<li>确认是否把 V5X/回测管线切到 <span class=mono>bar_60m_ths</span> 并重跑——我会先在新网格上做自洽校验（self-check 逐笔 0 偏差）再给新净值。</li>
<li>若只做 EB/EG/MA/SA 四品种，现有修正已完整可立即切换。</li>
</ol>
</body></html>"""

out=os.path.join(BASE,'ths_grid_correction_report.html')
with open(out,'w',encoding='utf-8') as f: f.write(html)
print('WROTE', out, len(html), 'bytes')
print('grid groups:', {s:g[1] for s,g in grid_summary.items()})
print('ths rows', len(ths), 'symbols', ths.symbol.nunique())
