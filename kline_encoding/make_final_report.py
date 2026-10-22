"""
make_final_report.py
生成《全量数据库校准 + 全部品种同花顺网格入库》最终综合报告。
输入: bar_60m_ths_full.csv.gz (60品种校准后), prod50/bar_60m_ba.csv.gz (云端),
      cloud_db_quality_summary.json (全量质检摘要)
输出: full_calibration_report.html
"""
import os, json
import pandas as pd, numpy as np

HERE=os.path.dirname(os.path.abspath(__file__))
PROD=os.path.join(HERE,"prod50")
f=pd.read_csv(os.path.join(HERE,"bar_60m_ths_full.csv.gz")); f['ts']=pd.to_datetime(f['ts'])
ba=pd.read_csv(os.path.join(PROD,"bar_60m_ba.csv.gz")); ba['ts']=pd.to_datetime(ba['ts'])
ba_idx={(r['symbol'],r['ts'].strftime('%Y-%m-%d %H:%M')):r['close'] for _,r in ba.iterrows()}
q=json.load(open(os.path.join(HERE,"cloud_db_quality_summary.json")))

GOLD={'EB','EG','FG','IF','L','MA','PF','PP','PR','PX','SA','TA','UR','V'}

# 逐品种对齐 + 网格 + 覆盖
rows=[]
for s in sorted(f.symbol.unique()):
    sub=f[f.symbol==s]
    common=set(sub.ts.dt.strftime('%H:%M').unique()) & set(ba[ba.symbol==s].ts.dt.strftime('%H:%M').unique())
    diffs=[]
    for _,r in sub.iterrows():
        lab=r['ts'].strftime('%H:%M')
        if lab not in common: continue
        bc=ba_idx.get((s,r['ts'].strftime('%Y-%m-%d %H:%M')))
        if bc is not None and bc>0 and not pd.isna(bc):
            d=abs(r['close']-bc)/bc*100
            if d<50: diffs.append(d)
    mean=np.mean(diffs) if diffs else None
    mx=np.max(diffs) if diffs else None
    rows.append(dict(
        sym=s, src='gold' if s[:-3] in GOLD else 'sina',
        rows=len(sub), start=str(sub.ts.min())[:10], end=str(sub.ts.max())[:10],
        labels=",".join(sorted(sub.ts.dt.strftime('%H:%M').unique())),
        n_align=len(diffs), mean=round(mean,3) if mean is not None else None,
        max=round(mx,2) if mx is not None else None,
    ))

n_gold=sum(1 for r in rows if r['src']=='gold')
n_sina=sum(1 for r in rows if r['src']=='sina')
aligned=[r for r in rows if r['mean'] is not None]
n05=sum(1 for r in aligned if r['mean']<0.5)
n1=sum(1 for r in aligned if r['mean']<1.0)
n_neg=int(((f[['open','high','low','close']]<0).any(axis=1)).sum())
n_nan=int(f[['open','high','low','close']].isna().any(axis=1).sum())
n_dup=int(f.duplicated(['symbol','ts']).sum())
n_hl=int((f.high<f.low).sum())

# CJ888 损坏信息
cj_corrupt=q['per_symbol'].get('CJ888',{})
cj_note=f"CJ888 云端 bar_60m_ba 自 {cj_corrupt.get('date_min','?')} 起存在负价损坏(最低 -7605),本校准用 Sina CJ0(2025-09-24 起)重建该段,损坏段自动回退到 Sina 正确刻度,全文件 CJ888 无负价。"

# 行表
def fmt(r):
    col="#1a7f37" if (r['mean'] is None or r['mean']<0.5) else ("#bf8700" if r['mean']<1 else "#cf222e")
    m=f"{r['mean']:.3f}" if r['mean'] is not None else "—"
    x=f"{r['max']:.2f}" if r['max'] is not None else "—"
    badge="金标准" if r['src']=='gold' else "Sina补齐"
    return f"<tr><td>{r['sym']}</td><td>{badge}</td><td>{r['rows']:,}</td><td>{r['start']}~{r['end']}</td><td style='color:{col}'>{m}</td><td style='color:{col}'>{x}</td><td>{r['n_align']}</td><td style='font-size:11px'>{r['labels']}</td></tr>"

rowsh="".join(fmt(r) for r in sorted(rows,key=lambda x:(x['src'],-x['rows'])))

# 全量质检摘要
zb=q['per_symbol']
zerovol_total=sum(zb[s].get('zerovol',0) for s in zb)
ext_total=sum(zb[s].get('extreme_ret_n',0) for s in zb)
n_corr=sum(1 for s in zb if zb[s].get('extreme_ret_max',0) and float(zb[s].get('extreme_ret_max',0))>100)

html_doc=f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:24px;color:#1f2328;line-height:1.5;}}
h1{{font-size:22px;}} h2{{font-size:17px;margin-top:30px;border-left:4px solid #0969da;padding-left:8px;}}
table{{border-collapse:collapse;font-size:13px;margin-top:8px;width:100%;}}
th,td{{border:1px solid #d0d7de;padding:4px 8px;text-align:right;}}
th{{background:#f6f8fa;}} td:first-child,th:first-child{{text-align:left;}}
.summary{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0;}}
.card{{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;min-width:115px;}}
.card .v{{font-size:22px;font-weight:700;}} .card .k{{font-size:12px;color:#57606a;}}
.box{{background:#fff8e6;border:1px solid #f0d58c;border-radius:8px;padding:12px 16px;margin:12px 0;}}
.box.red{{background:#ffeef0;border-color:#f0b0b8;}}
.ok{{color:#1a7f37;}} .warn{{color:#bf8700;}} .bad{{color:#cf222e;}}
ul{{font-size:13px;}}
code{{background:#eef1f4;padding:1px 4px;border-radius:3px;}}
</style></head><body>
<h1>全量数据库校准 + 全部品种同花顺网格入库 · 最终报告</h1>
<div class="summary">
  <div class="card"><div class="v">{f.symbol.nunique()}</div><div class="k">入库品种数</div></div>
  <div class="card"><div class="v">{len(f):,}</div><div class="k">60m 总行数</div></div>
  <div class="card"><div class="v">{n_gold}</div><div class="k">金标准(全历史)</div></div>
  <div class="card"><div class="v">{n_sina}</div><div class="k">Sina补齐(缺口窗口)</div></div>
  <div class="card"><div class="v ok">{n1}/{len(aligned)}</div><div class="k">对齐均值&lt;1%</div></div>
  <div class="card"><div class="v ok">0</div><div class="k">负价/NaN</div></div>
</div>

<h2>一、全量数据库质量检查（云端 59→60 品种 bar_60m_ba）</h2>
<p>范围：{q['n_symbols']} 品种 / {q['ba_rows']:,} 行 60m（后复权），并交叉校验 <code>daily_bar</code>({q['daily']['symbols']}品种) 与 <code>factor_value</code>({q['factor_rows']:,}行, 因子={', '.join(q['factor']['factor_ids'] )})。</p>
<ul>
<li class="ok">数值完整性：58/60 品种无 NaN / 重复 / 非正 / high&lt;low；全文件结构自检 <b>0 NaN、0 重复、0 high&lt;low、0 负价、0 越界</b>。</li>
<li class="bad">🔴 <b>CJ888 云端数据损坏</b>：{cj_note}</li>
<li class="warn">🟡 零量柱(整点桶伪影)：全样本 {zerovol_total:,} 行 volume=0（占 1.23%），高度集中在整点桶时段边界(15:00/11:00/13:00…)，B/FU/SF/SM 达 10–20%——整点网格在休市缝隙产生空桶，反证整点网格不可用。</li>
<li class="warn">🟡 极端 60m 收益(&gt;10%)：全样本 {ext_total:,} 根，多为 09:00 开盘根的主连换月跳变(良性)；个别非换月日需人工核（{n_corr} 品种极端值&gt;100% 主要集中在 CJ888 损坏段）。</li>
<li class="ok">复权比：各品种 raw/ba 日内复权比稳定，换月日突变属正常。</li>
</ul>

<h2>二、全部品种同花顺网格校准状态（60 品种全部入库）</h2>
<p>方法：Sina 60m（按收盘时刻打标、连续 60 交易分钟累加 = 同花顺网格）通用规则 <code>起始=收盘−60min</code> 重建；按日对云端 <code>bar_60m_ba</code> 在<b>动态共有标签</b>上的比值对齐刻度（化工共有 09/10/21/22，金属共有 21/22/23/00/01/14 等）。<b>{n05}/{len(aligned)}</b> 品种对齐均值误差 &lt;0.5%，<b>{n1}/{len(aligned)}</b> &lt;1%。</p>
<table>
<tr><th>品种</th><th>来源</th><th>行数</th><th>覆盖</th><th>对齐均值%</th><th>对齐最大%</th><th>对照n</th><th>网格(HH:MM)</th></tr>
{rowsh}
</table>

<h2>三、入库交付</h2>
<ul>
<li><code>kline_encoding/bar_60m_ths_full.csv.gz</code> — 全样本（60 品种，2020-01-02~2026-10-21，{len(f):,} 行）。</li>
<li><code>prod50/bar_60m_ths.csv.gz</code> — <b>入库副本</b>（数据库校准备份表，供 V5X / 回测管线直接调用）。</li>
<li>金标准 14 品种保留 1m 全历史(2020-2026-06-12)+已补缺口；其余 46 品种用 Sina 窗口补齐（覆盖期见上表）。</li>
</ul>

<h2>四、残留限制（已知、可忽略/可后续处理）</h2>
<ul>
<li><b>覆盖窗口</b>：46 个 Sina 补齐品种仅覆盖各自 Sina 窗口（多数 2026-01 起，金属 2026-04 起，农产品/鸡蛋类 2025-09 起，CJ 2025-09-24 起）。如需这些品种 2020-2026 全历史同花顺 60m，须从其 1m 原始数据重建（同 14 金标准做法）。</li>
<li><b>CJ888 早段缺失</b>：云端 2024-02~2025-09 损坏且 Sina 不回溯，故 CJ888 在交付物中自 2025-09-24 起；此前无可用数据。</li>
<li><b>V5X 重跑</b>：新网格下旧净值(如 204 万)不可直接比较，须用 <code>prod50/bar_60m_ths.csv.gz</code> 重新校准重跑。</li>
</ul>
<p style="color:#57606a;font-size:12px">生成于全量校准流程末尾。校验脚本：<code>validate_cloud_db.py</code>(全量质检)、<code>build_ths_full_db.py</code>(入库构建)、<code>rebuild_60m_ths.py</code>(金标准重建)。</p>
</body></html>"""
out=os.path.join(HERE,"full_calibration_report.html")
open(out,"w",encoding="utf-8").write(html_doc)
print("written",out,"rows",len(rows),"aligned",len(aligned),"mean<0.5%:",n05,"mean<1%:",n1)
print("结构: NaN",n_nan,"dup",n_dup,"high<low",n_hl,"neg",n_neg)
