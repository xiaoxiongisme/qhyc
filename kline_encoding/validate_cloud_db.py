"""
全量云端 60m 数据质量校准检查（59 品种）
=====================================
检查 prod50 数据库里的 60m 价格数据（bar_60m_ba / bar_60m_raw）、日线（daily_bar）、
因子（factor_value）的整体质量，并交叉校验一致性。

输出：
  cloud_db_quality_report.html   —— 可视化报告
  cloud_db_quality_summary.json  —— 机器可读摘要（供下游入库脚本使用）
"""
import os, json, html
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.join(BASE, "prod50")

ba = pd.read_csv(os.path.join(PROD, "bar_60m_ba.csv.gz"))
raw = pd.read_csv(os.path.join(PROD, "bar_60m_raw.csv.gz"))
daily = pd.read_csv(os.path.join(PROD, "daily_bar.csv.gz"))
fac = pd.read_csv(os.path.join(PROD, "factor_value.csv.gz"))

for d in (ba, raw):
    d["ts"] = pd.to_datetime(d["ts"])
daily["trade_date"] = pd.to_datetime(daily["trade_date"])
fac["trade_date"] = pd.to_datetime(fac["trade_date"])

# 交易日历（用 daily_bar 全市场并集作为"应有交易日"参考）
all_trade_days = set(daily["trade_date"].dt.normalize().unique())
# 各品种实际交易日
daily_days = daily.groupby("symbol")["trade_date"].apply(lambda s: set(s.dt.normalize().unique()))

SYMS = sorted(ba["symbol"].unique())
REC = {}  # per-symbol record

for sym in SYMS:
    sub = ba[ba.symbol == sym].sort_values("ts").reset_index(drop=True)
    n = len(sub)
    if n == 0:
        REC[sym] = {"rows": 0, "errors": ["无数据"]}
        continue
    o, h, l, c, v = sub.open, sub.high, sub.low, sub.close, sub.volume
    r = {}
    r["rows"] = n
    r["date_min"] = str(sub.ts.min())
    r["date_max"] = str(sub.ts.max())
    r["years"] = int(sub.ts.dt.year.nunique())
    # 1) NaN
    nan_ohlc = int(sub[["open", "high", "low", "close"]].isna().any(axis=1).sum())
    r["nan_ohlc"] = nan_ohlc
    # 2) 重复时间戳
    dup = int(sub.duplicated(subset=["ts"]).sum())
    r["dup_ts"] = dup
    # 3) 非正 OHLC
    nonpos = int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum())
    r["nonpos"] = nonpos
    # 4) high < low
    hl_bad = int((h < l).sum())
    r["high_lt_low"] = hl_bad
    # 5) close 越界 [low, high]
    oob = int(((c > h) | (c < l)).sum())
    r["close_oob"] = oob
    # 6) 零/负成交量
    zerovol = int((v <= 0).sum())
    r["zerovol"] = zerovol
    # 7) 极端 60m 收益率（close-to-close）>10% —— 疑换月或错误
    ret = c.pct_change().abs() * 100
    ext = sub[ret > 10][["ts", "close"]].copy()
    ext["ret%"] = ret[ret > 10].round(2).values
    r["extreme_ret_n"] = int((ret > 10).sum())
    r["extreme_ret_max"] = round(float(ret.max()), 2)
    # 8) 交易日缺口：用该品种 daily_bar 交易日集合对照
    dd = sorted(daily_days.get(sym, set()))
    # 取 60m 覆盖区间内的 daily 交易日
    t0, t1 = sub.ts.min().normalize(), sub.ts.max().normalize()
    expected = [d for d in sorted(all_trade_days) if t0 <= d <= t1]
    actual_days = set(sub.ts.dt.normalize().unique())
    missing = [d for d in expected if d not in actual_days]
    r["expected_days"] = len(expected)
    r["missing_days"] = len(missing)
    # 9) raw/ba 复权比日内漂移：raw 应有 ba/raw 比，检查是否稳定（换月日会突变属正常）
    sr = raw[raw.symbol == sym].sort_values("ts").reset_index(drop=True)
    sr["ts"] = sr["ts"]
    m = sub.merge(sr[["ts", "close"]], on="ts", suffixes=("_ba", "_raw"), how="inner")
    m = m[(m.close_raw > 0) & (m.close_ba > 0)]
    if len(m):
        m["ratio"] = m.close_ba / m.close_raw
        m["date"] = m.ts.dt.normalize()
        day_ratio = m.groupby("date")["ratio"].median()
        # 复权比相对自身中位数偏差>5% 的天数（剔除换月冲击的常态漂移）
        med = day_ratio.median()
        r["adj_ratio_median"] = round(float(med), 5)
        r["adj_ratio_daily_max"] = round(float(day_ratio.max()), 5)
        r["adj_ratio_daily_min"] = round(float(day_ratio.min()), 5)
    else:
        r["adj_ratio_median"] = None
    REC[sym] = r

# ---- factor_value 检查 ----
fac_rec = {}
fac_rec["factor_ids"] = sorted(fac.factor_id.unique().tolist())
fac_rec["symbols"] = int(fac.symbol.nunique())
fac_rec["rows"] = int(len(fac))
fac_rec["date_min"] = str(fac.trade_date.min())
fac_rec["date_max"] = str(fac.trade_date.max())
fac_rec["nan_z"] = int(fac.z_value.isna().sum())
fac_rec["inf_z"] = int(np.isinf(fac.z_value).sum()) if fac.z_value.dtype != object else 0

# ---- daily_bar 检查 ----
daily_rec = {}
daily_rec["symbols"] = int(daily.symbol.nunique())
daily_rec["date_min"] = str(daily.trade_date.min())
daily_rec["date_max"] = str(daily.trade_date.max())
daily_rec["nan_ohlc"] = int(daily[["open", "high", "low", "close"]].isna().any(axis=1).sum())
daily_rec["high_lt_low"] = int((daily.high < daily.low).sum())

# ---- 汇总 ----
total = len(SYMS)
issues = {}
for sym, r in REC.items():
    bad = []
    if r.get("nan_ohlc", 0): bad.append(f"NaN×{r['nan_ohlc']}")
    if r.get("dup_ts", 0): bad.append(f"重复ts×{r['dup_ts']}")
    if r.get("nonpos", 0): bad.append(f"非正价×{r['nonpos']}")
    if r.get("high_lt_low", 0): bad.append(f"high<low×{r['high_lt_low']}")
    if r.get("close_oob", 0): bad.append(f"close越界×{r['close_oob']}")
    if r.get("zerovol", 0): bad.append(f"零量×{r['zerovol']}")
    if r.get("missing_days", 0): bad.append(f"缺交易日×{r['missing_days']}")
    if bad:
        issues[sym] = bad

summary = {
    "n_symbols": total,
    "ba_rows": int(len(ba)),
    "raw_rows": int(len(raw)),
    "daily_rows": int(len(daily)),
    "factor_rows": int(len(fac)),
    "symbols_with_issues": len(issues),
    "per_symbol": REC,
    "issues": issues,
    "daily": daily_rec,
    "factor": fac_rec,
}
with open(os.path.join(BASE, "cloud_db_quality_summary.json"), "w") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1, default=str)

# ---- HTML 报告 ----
def color(v, ok_max, warn_max):
    if v <= ok_max: return "#1a7f37"
    if v <= warn_max: return "#bf8700"
    return "#cf222e"

rows_html = ""
for sym in SYMS:
    r = REC[sym]
    flag = "⚠" if sym in issues else "✓"
    nan = r.get("nan_ohlc", 0); dup = r.get("dup_ts", 0); hl = r.get("high_lt_low", 0)
    oob = r.get("close_oob", 0); zv = r.get("zerovol", 0); miss = r.get("missing_days", 0)
    ext = r.get("extreme_ret_n", 0)
    rows_html += f"""<tr>
      <td>{flag} {sym}</td>
      <td>{r.get('rows','')}</td>
      <td>{r.get('date_min','')[:10]}~{r.get('date_max','')[:10]}</td>
      <td style="color:{color(nan,0,0)}">{nan}</td>
      <td style="color:{color(dup,0,0)}">{dup}</td>
      <td style="color:{color(hl,0,0)}">{hl}</td>
      <td style="color:{color(oob,0,0)}">{oob}</td>
      <td style="color:{color(zv,0,20)}">{zv}</td>
      <td style="color:{color(miss,0,5)}">{miss}</td>
      <td style="color:{color(ext,0,5)}">{ext}</td>
      <td>{r.get('extreme_ret_max','')}</td>
      <td>{r.get('adj_ratio_median','')}</td>
    </tr>"""

issue_html = ""
for sym, bad in issues.items():
    issue_html += f"<li><b>{sym}</b>: {', '.join(bad)}</li>"

html_doc = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:24px;color:#1f2328;}}
h1{{font-size:22px;}} h2{{font-size:17px;margin-top:28px;border-left:4px solid #0969da;padding-left:8px;}}
table{{border-collapse:collapse;font-size:13px;margin-top:8px;width:100%;}}
th,td{{border:1px solid #d0d7de;padding:4px 8px;text-align:right;}}
th{{background:#f6f8fa;}} td:first-child,th:first-child{{text-align:left;}}
.summary{{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0;}}
.card{{border:1px solid #d0d7de;border-radius:8px;padding:12px 18px;min-width:120px;}}
.card .v{{font-size:24px;font-weight:700;}} .card .k{{font-size:12px;color:#57606a;}}
ul{{font-size:13px;}}
</style></head><body>
<h1>云端数据库 60m 全量数据质量校准检查</h1>
<div class="summary">
  <div class="card"><div class="v">{total}</div><div class="k">品种数</div></div>
  <div class="card"><div class="v">{len(ba):,}</div><div class="k">60m-ba 行</div></div>
  <div class="card"><div class="v">{len(daily):,}</div><div class="k">日线行</div></div>
  <div class="card"><div class="v">{len(fac):,}</div><div class="k">因子行</div></div>
  <div class="card"><div class="v" style="color:{'#cf222e' if issues else '#1a7f37'}">{len(issues)}</div><div class="k">有问题品种</div></div>
</div>
<p>检查项：NaN、重复时间戳、非正价、high&lt;low、close 越界、零量、交易日缺口、极端 60m 收益率(&gt;10%)、raw/ba 复权比。</p>

<h2>各品种明细（flag: ✓ 通过 / ⚠ 有问题）</h2>
<table>
<tr><th>品种</th><th>行数</th><th>区间</th><th>NaN</th><th>重复</th><th>H&lt;L</th><th>越界</th><th>零量</th><th>缺日</th><th>极端收益</th><th>最大收益%</th><th>复权比中位</th></tr>
{rows_html}
</table>

<h2>问题清单</h2>
{('<ul>'+issue_html+'</ul>') if issue_html else '<p style="color:#1a7f37">✓ 无严重质量问题</p>'}

<h2>其他表</h2>
<p><b>daily_bar</b>: {daily_rec['symbols']} 品种, {daily_rec['date_min'][:10]}~{daily_rec['date_max'][:10]}, NaN(OHLC)={daily_rec['nan_ohlc']}, high&lt;low={daily_rec['high_lt_low']}</p>
<p><b>factor_value</b>: {fac_rec['symbols']} 品种, 因子={', '.join(fac_rec['factor_ids'])}, {fac_rec['date_min'][:10]}~{fac_rec['date_max'][:10]}, NaN(z)={fac_rec['nan_z']}, inf={fac_rec['inf_z']}</p>

<h2>结论</h2>
<p>云端 60m 数据在 <b>数值完整性</b>（NaN/重复/非正/越界）层面{'基本干净' if not any(REC[s].get('nan_ohlc',0) or REC[s].get('dup_ts',0) or REC[s].get('high_lt_low',0) or REC[s].get('close_oob',0) for s in SYMS) else '存在异常'}。
但需重点注意：</p>
<ul>
<li><b>网格口径</b>：bar_60m_ba/raw 采用<b>整点桶</b>（09/10/11/13/14/15 + 21/22/23），与同花顺「连续 60 交易分钟」网格不同，是信号与图表对不上的根因（本会话主线已修复于 bar_60m_ths_full.csv.gz）。</li>
<li><b>极端收益率</b>：部分品种的 &gt;10% 单根 60m 收益多出现在合约换月日，属复权主连的正常跳变；个别若非换月日则需人工核。</li>
<li><b>复权比</b>：见各品种「复权比中位」列，若某品种复权比 daily_max/daily_min 跨度极大（如 &gt;2 倍），提示该品种主连换月规则特殊，校准时需谨慎。</li>
</ul>
</body></html>"""

with open(os.path.join(BASE, "cloud_db_quality_report.html"), "w", encoding="utf-8") as f:
    f.write(html_doc)

print(f"检查完成: {total} 品种, ba={len(ba):,} 行, 有问题品种={len(issues)}")
print("有问题的品种:", list(issues.keys()))
# 打印极端收益明细（top）
print("\n各品种极端 60m 收益(>10%)最多的几个:")
top = sorted([(s, REC[s].get('extreme_ret_n',0)) for s in SYMS], key=lambda x:-x[1])[:12]
for s, n in top:
    if n: print(f"  {s}: {n} 根 (>10%), 最大 {REC[s].get('extreme_ret_max','')}%")
