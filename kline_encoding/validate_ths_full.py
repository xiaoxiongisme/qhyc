"""
validate_ths_full.py
对 bar_60m_ths_full.csv.gz（同花顺网格全样本 60m）做整体校准与检查：
  1) 结构校验：网格/去重/单调/OHLC 自洽/NaN/volume<=0
  2) 覆盖校验：每日根数分布、低于众数的异常日
  3) 校准校验：vs 云端 bar_60m_ba（MA888 后复权）overlapping 标签的收盘价差
  4) 接缝校验：2026-06-12/06-13 边界连续性
  5) 异常跳空：intraday（相邻 <=200min）>3% 的离群跳
  6) PR/SC 专项（grid_verified=False）：对齐量化
产出 data_calibration_report.html
"""
import pandas as pd, numpy as np, os, html, json, datetime as dtmod

HERE = os.path.dirname(os.path.abspath(__file__))
FULL   = os.path.join(HERE, "bar_60m_ths_full.csv.gz")
PRE    = os.path.join(HERE, "bar_60m_ths.csv.gz")        # 1m 金标准(2020~2026-06-12)
GAP    = os.path.join(HERE, "bar_60m_ths_gap.csv.gz")
BA     = os.path.join(HERE, "prod50", "bar_60m_ba.csv.gz")  # 云端参考(整数网格, 后复权)

SEAM_DATE = "2026-06-13"
SEAM_TS = pd.Timestamp(SEAM_DATE)
INTRADAY_MAX_DT = 200   # min
JUMP_TH = 3.0            # %

full = pd.read_csv(FULL); full['ts']=pd.to_datetime(full['ts'])
ba   = pd.read_csv(BA);   ba['ts']=pd.to_datetime(ba['ts'])
pre  = pd.read_csv(PRE);  pre['ts']=pd.to_datetime(pre['ts'])
gap  = pd.read_csv(GAP);  gap['ts']=pd.to_datetime(gap['ts'])

R = {}  # report dict

# ---------- 1. 结构校验 ----------
struct = []
for s in sorted(full.symbol.unique()):
    sub = full[full.symbol==s].sort_values('ts')
    n = len(sub)
    hh = sorted(sub.ts.dt.strftime('%H:%M').unique())
    perday = sub.groupby(sub.ts.dt.date).size().mode().iloc[0]
    dup = sub.duplicated(subset=['ts']).sum()
    # monotonic
    mono = (sub.ts.diff().dropna().dt.total_seconds() <= 0).sum()
    nan_ohlc = sub[['open','high','low','close']].isna().any(axis=1).sum()
    vol0 = (sub.volume<=0).sum()
    bad_hl = (sub.high < sub.low).sum()
    bad_out = ((sub.open>sub.high)|(sub.open<sub.low)|(sub.close>sub.high)|(sub.close<sub.low)).sum()
    struct.append(dict(symbol=s, n=n, bars_per_day=int(perday), labels=" ".join(hh),
                       dup=int(dup), mono_viol=int(mono), nan_ohlc=int(nan_ohlc),
                       vol0=int(vol0), bad_hl=int(bad_hl), bad_out=int(bad_out),
                       verified=bool(sub.grid_verified.any())))
R['struct'] = struct

# ---------- 2. 覆盖校验 ----------
cover = []
for s in sorted(full.symbol.unique()):
    sub = full[full.symbol==s]
    cnt = sub.groupby(sub.ts.dt.date).size()
    mode = cnt.mode().iloc[0]
    below = cnt[cnt < mode]
    cover.append(dict(symbol=s, mode=int(mode), ndays=len(cnt),
                      below=int((cnt<mode).sum()),
                      below_examples="; ".join(sorted(set(d.strftime('%Y-%m-%d') for d in below.index))[:200])))
R['cover'] = cover

# ---------- 3. 校准校验 vs cloud ba (dynamic overlapping labels) ----------
ba_idx = {}
for _,r in ba.iterrows():
    ba_idx[(r['symbol'], r['ts'].strftime('%Y-%m-%d %H:%M'))] = r['close']
calib = []
for s in sorted(full.symbol.unique()):
    sub = full[full.symbol==s].copy()
    full_labels = set(sub.ts.dt.strftime('%H:%M').unique())
    ba_labels = set(ba[ba.symbol==s].ts.dt.strftime('%H:%M').unique())
    common = sorted(full_labels & ba_labels)
    if not common:
        calib.append(dict(symbol=s, common=",".join(common) or "—", n=0,
                         mean=0.0, max=0.0, mean_pre=0.0, mean_gap=0.0))
        continue
    diffs, diffs_pre, diffs_gap = [], [], []
    for _,r in sub.iterrows():
        lab = r['ts'].strftime('%H:%M')
        if lab not in common: continue
        bk = (s, r['ts'].strftime('%Y-%m-%d %H:%M'))
        if bk in ba_idx and ba_idx[bk] not in (0, None) and not pd.isna(ba_idx[bk]):
            d = abs(r['close']-ba_idx[bk])/ba_idx[bk]*100
            diffs.append(d)
            if r['ts'] < SEAM_TS: diffs_pre.append(d)
            else: diffs_gap.append(d)
    mean = np.mean(diffs) if diffs else float('nan')
    mx = np.max(diffs) if diffs else float('nan')
    mp = np.mean(diffs_pre) if diffs_pre else float('nan')
    mg = np.mean(diffs_gap) if diffs_gap else float('nan')
    calib.append(dict(symbol=s, common=",".join(common), n=len(diffs),
                     mean=round(mean,3), max=round(mx,3),
                     mean_pre=round(mp,3), mean_gap=round(mg,3)))
R['calib'] = calib

# ---------- 4. 接缝校验 ----------
seam = []
for s in sorted(full.symbol.unique()):
    sub = full[full.symbol==s].sort_values('ts')
    pre_part = sub[sub.ts < SEAM_TS]; post = sub[sub.ts >= SEAM_TS]
    if len(pre_part) and len(post):
        last = pre_part.iloc[-1]; first = post.iloc[0]
        jump = abs(first['open']-last['close'])/last['close']*100
        seam.append(dict(symbol=s,
                         last=f"{last.ts:%Y-%m-%d %H:%M} c{last['close']:.0f}",
                         first=f"{first.ts:%Y-%m-%d %H:%M} o{first['open']:.0f}",
                         jump=round(jump,2)))
R['seam'] = seam

# ---------- 5. 异常跳空 ----------
jumps_all=[]; jumps_pre=[]; jumps_gap=[]
jump_rows=[]
for s in sorted(full.symbol.unique()):
    sub = full[full.symbol==s].sort_values('ts').reset_index(drop=True)
    sub['dt_min'] = sub.ts.diff().dt.total_seconds()/60
    sub['ret'] = sub['close'].pct_change()*100
    intra = sub['dt_min'] <= INTRADAY_MAX_DT
    flag = intra & sub['ret'].abs().gt(JUMP_TH)
    for i in sub[flag].index:
        r = sub.loc[i]
        jumps_all.append(abs(r['ret']))
        (jumps_pre if r['ts']<SEAM_TS else jumps_gap).append(abs(r['ret']))
        jump_rows.append(dict(symbol=s, ts=f"{r.ts:%Y-%m-%d %H:%M}", dt=round(r['dt_min'],0),
                              ret=round(r['ret'],2)))
R['jumps'] = dict(n_all=len(jumps_all), n_pre=len(jumps_pre), n_gap=len(jumps_gap),
                  p95=round(np.percentile(jumps_all,95),2) if jumps_all else 0,
                  top=sorted(jump_rows, key=lambda x:-abs(x['ret']))[:25])
R['jump_th']=JUMP_TH

# ---------- 6. PR/SC 专项 ----------
prsc = [c for c in R['calib'] if c['symbol'] in ('PR888','SC888')]
R['prsc'] = prsc

# ---------- summary scorecard ----------
R['meta'] = dict(
    rows=len(full), symbols=full.symbol.nunique(),
    range=f"{full.ts.min():%Y-%m-%d} ~ {full.ts.max():%Y-%m-%d}",
    pre_rows=int((full.ts<SEAM_TS).sum()), gap_rows=int((full.ts>=SEAM_TS).sum()),
    gen=dtmod.datetime.now().strftime('%Y-%m-%d %H:%M'),
)
# overall pass/fail
tot_nan = sum(c['nan_ohlc'] for c in struct)
tot_dup = sum(c['dup'] for c in struct)
tot_badhl = sum(c['bad_hl'] for c in struct)
tot_badout = sum(c['bad_out'] for c in struct)
max_calib_mean = max((c['mean'] for c in calib if c['n']>0), default=0)
R['score'] = dict(tot_nan=tot_nan, tot_dup=tot_dup, tot_badhl=tot_badhl, tot_badout=tot_badout,
                  max_calib_mean=round(max_calib_mean,3),
                  ok = (tot_nan==0 and tot_dup==0 and tot_badhl==0 and tot_badout==0))

# ===================== HTML =====================
def tbl(rows, cols):
    th = "".join(f"<th>{c}</th>" for c in cols)
    trs=""
    for r in rows:
        tds=""
        for c in cols:
            v=r[c]
            cls=""
            if isinstance(v,(int,float)) and c in ('nan_ohlc','dup','bad_hl','bad_out','mono_viol','vol0','below','n'):
                if c in ('nan_ohlc','dup','bad_hl','bad_out','mono_viol') and isinstance(v,int) and v>0: cls=' class="bad"'
            tds+=f"<td{cls}>{v}</td>"
        trs+=f"<tr>{tds}</tr>"
    return f"<table><tr>{th}</tr>{trs}</table>"

m=R['meta']; sc=R['score']
scorecard = f"""
<div class="card">
  <h2>总览</h2>
  <ul>
    <li>全样本行数 <b>{m['rows']:,}</b> · 品种 <b>{m['symbols']}</b> · 区间 <b>{m['range']}</b></li>
    <li>pre-gap(1m金标准) <b>{m['pre_rows']:,}</b> 根 · gap(Sina补齐) <b>{m['gap_rows']:,}</b> 根</li>
    <li>生成时间 {m['gen']}</li>
  </ul>
  <p class="verdict {'ok' if sc['ok'] else 'warn'}">
    结构自检：NaN-OHLC {sc['tot_nan']} · 重复(symbol,ts) {sc['tot_dup']} · high&lt;low {sc['tot_badhl']} · 价格越界 {sc['tot_badout']}<br>
    最大校准误差(全窗口均值) <b>{sc['max_calib_mean']}%</b> → {'✅ 通过' if sc['ok'] else '⚠️ 存在结构问题'}
  </p>
</div>"""

sec1 = "<div class='card'><h2>1. 结构校验</h2>"+tbl(R['struct'],
      ['symbol','n','bars_per_day','labels','dup','mono_viol','nan_ohlc','vol0','bad_hl','bad_out','verified'])+"</div>"

sec2 = "<div class='card'><h2>2. 覆盖校验（每日根数）</h2>"+tbl(R['cover'],
      ['symbol','mode','ndays','below','below_examples'])+"</div>"

sec3 = "<div class='card'><h2>3. 校准校验 vs 云端 bar_60m_ba（MA888 后复权）</h2>"+\
       "<p>dynamic overlapping 标签（同花顺网格 ∩ 云端整数网格，通常 09:00/10:00/21:00/22:00）。"+\
       "mean=全窗口收盘价差均值，mean_pre=缺口前，mean_gap=缺口期。</p>"+\
       tbl(R['calib'],['symbol','common','n','mean','max','mean_pre','mean_gap'])+"</div>"

sec4 = "<div class='card'><h2>4. 接缝校验（2026-06-12→06-13）</h2>"+tbl(R['seam'],
      ['symbol','last','first','jump'])+"</div>"

j=R['jumps']
sec5 = f"<div class='card'><h2>5. 异常跳空（intraday 相邻≤{INTRADAY_MAX_DT}min 且 |收益|>{JUMP_TH}%）</h2>"+\
       f"<p>intraday 跳总数 <b>{j['n_all']}</b>（pre {j['n_pre']} / gap {j['n_gap']}），95分位 {j['p95']}%。"+\
       "下列为 top 离群（pre 金标准与 gap 补齐均有，若 gap 占比高则需复核 Sina 复权比）。</p>"+\
       tbl(j['top'],['symbol','ts','dt','ret'])+"</div>"

p=R['prsc']
sec6 = "<div class='card'><h2>6. PR / SC 专项（grid_verified=False）</h2>"+\
       "<p>这两品种未在 build 脚本中设置标签映射，缺口期直接用 Sina 收盘标签-60min 当起始标签，需人工核网格。"+\
       "下表为它们与云端 ba 在对齐标签上的误差，若误差小说明 Sina 标签恰与同花顺一致，否则须补映射。</p>"+\
       tbl(p,['symbol','common','n','mean','max','mean_pre','mean_gap'])+"</div>"

html_doc = f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;margin:24px;color:#1a1a1a;background:#fafafa}}
h1{{font-size:22px;border-bottom:3px solid #2b6cb0;padding-bottom:8px}}
h2{{font-size:17px;color:#2b6cb0;margin-top:24px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{border:1px solid #e2e8f0;padding:5px 8px;text-align:left}}
th{{background:#edf2f7}}
tr:nth-child(even){{background:#f7fafc}}
.bad{{color:#c53030;font-weight:700}}
.verdict{{padding:10px;border-radius:6px;font-size:14px;margin-top:8px}}
.ok{{background:#f0fff4;border:1px solid #9ae6b4}}
.warn{{background:#fffaf0;border:1px solid #f6e05e}}
ul{{margin:6px 0;line-height:1.6}}
</style></head><body>
<h1>同花顺网格 60m 全样本 · 数据校准与检查报告</h1>
{scorecard}{sec1}{sec2}{sec3}{sec4}{sec5}{sec6}
<p style="color:#718096;font-size:12px">数据源：bar_60m_ths_full.csv.gz │ 参考：prod50/bar_60m_ba.csv.gz │ 脚本：validate_ths_full.py</p>
</body></html>"""

out = os.path.join(HERE, "data_calibration_report.html")
with open(out,"w",encoding="utf-8") as f: f.write(html_doc)

# console summary
print(f"FULL: rows={m['rows']:,} symbols={m['symbols']} {m['range']}")
print(f"STRUCT ok={sc['ok']}  nan={sc['tot_nan']} dup={sc['tot_dup']} badhl={sc['tot_badhl']} badout={sc['tot_badout']}")
print("\nCALIBRATION (mean close-diff % vs ba, full/pre/gap):")
for c in R['calib']:
    if c['n']>0:
        print(f"  {c['symbol']:6} n={c['n']:5} mean={c['mean']:6.3f} max={c['max']:6.3f}  pre={c['mean_pre']:6.3f} gap={c['mean_gap']:6.3f}  labels[{c['common']}]")
    else:
        print(f"  {c['symbol']:6} NO overlapping labels")
print(f"\nJUMPS: all={j['n_all']} pre={j['n_pre']} gap={j['n_gap']} p95={j['p95']}%")
print("  top jumps:")
for r in R['jumps']['top'][:10]:
    print(f"    {r['symbol']} {r['ts']} dt={r['dt']:.0f} ret={r['ret']:.2f}%")
print(f"\nSEAM max jump%: {max(x['jump'] for x in R['seam']):.2f}")
for x in R['seam']:
    if x['jump']>3: print(f"   !! {x['symbol']} {x['last']}->{x['first']} jump={x['jump']}%")
print(f"\nREPORT -> {out}")
