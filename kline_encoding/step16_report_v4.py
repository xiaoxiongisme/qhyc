# -*- coding: utf-8 -*-
"""Step 16: V4 报告 —— 三项任务 + 2026 holdout 一次性验证"""
import json

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
T12 = json.load(open(f"{OUT}/results_task12.json", encoding="utf-8"))
QV = json.load(open(f"{OUT}/results_qv.json", encoding="utf-8"))
V2 = json.load(open(f"{OUT}/results_verify2.json", encoding="utf-8"))
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
CORE = ["010000", "000000", "000100", "010001"]
RED, GREEN, SUB, INK = "#d63031", "#00997a", "#8a94a6", "#2c3e50"

def fmt_tbl(headers, rows):
    h = "".join(f"<th>{x}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"

def cell(mean, t=None, n=None, good_positive=None):
    if mean is None: return '<td class="mut">—</td>'
    if good_positive is None: good_positive = mean > 0
    col = RED if good_positive else GREEN
    s = f"{mean:+.4f}%"
    tt = f"<span class='tt'>t={t:+.1f}</span>" if t is not None else ""
    nn = f"<span class='nn'>n={n:,}</span>" if n is not None else ""
    return f'<td style="color:{col};font-weight:600">{s}{tt}{nn}</td>'

# ============ 图1: 任务① 单特征 R² 对比 ============
def fig_r2():
    ma = T12["features"]["MA"]
    feats = [("body_up", "实体方向(阳)"), ("has_upper", "上影有无"), ("has_lower", "下影有无")]
    groups = [("影线组合4态", "r2_shadow4"), ("实体2态", "r2_body2"), ("CLV20箱", "r2_clv20"), ("CLV×实体", "r2_clv_x_body"), ("形态码8态", "r2_code8")]
    W, H = 680, 300; x0, y0, cw, ch = 200, 52, 460, 200
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">对下一根涨跌幅的解释力 R²（甲醇，严格相邻口径）</text>')
    vmax = 0.028
    for g in range(8):
        v = vmax * g / 7
        xx = x0 + cw * v / vmax
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+14}" font-size="9" fill="{SUB}" text-anchor="middle">{v:.3f}</text>')
    bw = ch / (len(feats) + len(groups) + 1)
    i = 0
    for fkey, fname in feats:
        yy = y0 + bw * (i + 0.5) + 4
        vd = ma[f"{fkey}_r2_disc_2020_2025"]; vh = ma[f"{fkey}_r2_holdout_2026"]
        p.append(f'<text x="{x0-8}" y="{yy+4:.0f}" font-size="11" fill="{INK}" text-anchor="end">{fname}</text>')
        p.append(f'<rect x="{x0}" y="{yy-7:.0f}" width="{cw*vd/vmax:.0f}" height="12" rx="2" fill="#2e6fb7" fill-opacity="0.85"/>')
        p.append(f'<rect x="{x0}" y="{yy+6:.0f}" width="{cw*vh/vmax:.0f}" height="5" rx="1.5" fill="#2e6fb7" fill-opacity="0.35"/>')
        p.append(f'<text x="{x0+cw*vd/vmax+6:.0f}" y="{yy+2:.0f}" font-size="9.5" fill="#2e6fb7">{vd:.4f} / 26:{vh:.4f}</text>')
        i += 1
    p.append(f'<line x1="{x0-10}" y1="{y0+bw*3+6}" x2="{x0+cw}" y2="{y0+bw*3+6}" stroke="#d4d9e2" stroke-dasharray="3,3"/>')
    for gname, gkey in groups:
        yy = y0 + bw * (i + 0.5) + 4
        vd = ma[f"{gkey}_disc_2020_2025"]; vh = ma[f"{gkey}_holdout_2026"]
        p.append(f'<text x="{x0-8}" y="{yy+4:.0f}" font-size="11" font-weight="700" fill="{INK}" text-anchor="end">{gname}</text>')
        p.append(f'<rect x="{x0}" y="{yy-7:.0f}" width="{cw*vd/vmax:.0f}" height="12" rx="2" fill="#d63031" fill-opacity="0.85"/>')
        p.append(f'<rect x="{x0}" y="{yy+6:.0f}" width="{cw*vh/vmax:.0f}" height="5" rx="1.5" fill="#d63031" fill-opacity="0.35"/>')
        p.append(f'<text x="{x0+cw*vd/vmax+6:.0f}" y="{yy+2:.0f}" font-size="9.5" fill="{RED}">{vd:.4f} / 26:{vh:.4f}</text>')
        i += 1
    p.append(f'<rect x="{x0}" y="{y0+ch+24}" width="12" height="10" fill="#2e6fb7"/><text x="{x0+18}" y="{y0+ch+33}" font-size="10" fill="{INK}">单特征</text>')
    p.append(f'<rect x="{x0+80}" y="{y0+ch+24}" width="12" height="10" fill="#d63031"/><text x="{x0+98}" y="{y0+ch+33}" font-size="10" fill="{INK}">组合特征</text>')
    p.append(f'<rect x="{x0+170}" y="{y0+ch+24}" width="12" height="5" fill="#8a94a6" fill-opacity="0.4"/><text x="{x0+188}" y="{y0+ch+33}" font-size="10" fill="{INK}">细条=2026 holdout</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图2: 形态码→下一根振幅 (regime 表) ============
def fig_regime():
    ma = T12["regime"]["MA"]; eg = T12["regime"]["EG"]
    W, H = 680, 300; x0, y0, cw, ch = 130, 56, 520, 190
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">形态码 → 下一根振幅（波动率regime表）：粗=发现期 细=2026 holdout，排序完全保持</text>')
    vmax = 0.24
    for g in range(7):
        v = vmax * g / 6
        xx = x0 + cw * v / vmax
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+14}" font-size="9" fill="{SUB}" text-anchor="middle">{v:.2f}%</text>')
    bw = cw / 8
    for i, s in enumerate(STATES):
        xx = x0 + bw * i + bw * 0.2; w = bw * 0.6
        a = ma["amp_disc_2020_2025"][s]["mean_amp"]; b = ma["amp_holdout_2026"][s]["mean_amp"]
        hh1 = a / 100 / vmax * ch; hh2 = b / 100 / vmax * ch
        p.append(f'<rect x="{xx:.0f}" y="{y0+ch-hh1:.1f}" width="{w:.0f}" height="{hh1:.1f}" rx="2" fill="{RED}" fill-opacity="0.85"/>')
        p.append(f'<rect x="{xx:.0f}" y="{y0+ch-hh2:.1f}" width="{w:.0f}" height="{hh2:.1f}" rx="2" fill="none" stroke="{RED}" stroke-width="1.6" stroke-dasharray="3,2"/>')
        p.append(f'<text x="{xx+w/2:.0f}" y="{y0+ch-hh1-6:.1f}" font-size="9.5" font-weight="700" fill="{RED}" text-anchor="middle">{a:.3f}</text>')
        p.append(f'<text x="{xx+w/2:.0f}" y="{y0+ch+28}" font-size="9.5" font-family="monospace" fill="{INK}" text-anchor="middle">{s[:2]}·{s[2:4]}·{s[4:]}</text>')
    p.append(f'<text x="{x0+cw/2}" y="{y0+ch+46}" font-size="10" fill="{SUB}" text-anchor="middle">实心=发现期2020-2025（MA）  虚线框=2026 holdout —— 无影光头(010000/000000)=低波态, 双影阴(000101)=高波态, 两期排序完全一致</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图3: 量比分档扫描热力图 ============
def fig_volscan():
    ma = QV["scan"]["MA"]
    bins = ["Q1最小", "Q2", "Q3", "Q4", "Q5最大"]
    W, H = 680, 300; x0, y0, cw, ch = 120, 56, 540, 200
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">量比五档扫描：8形态×量比分位的下一根涨跌幅（甲醇，发现期2020-2024）</text>')
    for j, bn in enumerate(bins):
        p.append(f'<text x="{x0+cw*(j+0.5)/5:.0f}" y="{y0-8}" font-size="10" fill="{SUB}" text-anchor="middle">{bn}</text>')
    bw = cw / 5; bh = ch / 8
    for i, s in enumerate(STATES):
        p.append(f'<text x="{x0-8}" y="{y0+bh*(i+0.5)+4:.0f}" font-size="10.5" font-family="monospace" fill="{INK}" text-anchor="end">{s[:2]}·{s[2:4]}·{s[4:]}</text>')
        for j, bn in enumerate(bins):
            v = ma[s].get(bn)
            if not v: continue
            mv = v["mean"]
            alpha = min(abs(mv) / 0.025, 0.75)
            col = RED if mv > 0 else GREEN
            p.append(f'<rect x="{x0+bw*j+2:.0f}" y="{y0+bh*i+2:.0f}" width="{bw-4:.0f}" height="{bh-4:.0f}" rx="4" fill="{col}" fill-opacity="{alpha:.2f}" stroke="{col}" stroke-opacity="0.35"/>')
            p.append(f'<text x="{x0+bw*(j+0.5):.0f}" y="{y0+bh*(i+0.5)+4:.0f}" font-size="10" font-weight="600" fill="{INK}" text-anchor="middle">{mv:+.4f}</text>')
    p.append(f'<text x="{x0+cw/2}" y="{y0+ch+18}" font-size="10" fill="{SUB}" text-anchor="middle">色深=|效应|；四类核心信号在五档量比下效应几乎恒定（不挑量），唯一例外是 010100（Q1 +0.0120% → Q5 -0.0027%）——但该调制未通过2026 holdout</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图4: 锁定规则三级验证 ============
def fig_3tier():
    ma = QV["econ"]["MA"]["locked"]
    rules = [(c["rule"], c) for c in ma if c["type"] in ("base", "vol_binary", "vol_bin", "triple")]
    W, H = 680, 330; x0, y0, cw, ch = 210, 52, 450, 240
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">锁定规则三级验证（甲醇）：发现期 → 2025 → 2026 holdout，方向全部一致</text>')
    vmax = 0.03
    for g in range(7):
        v = -vmax + 2 * vmax * g / 6
        xx = x0 + cw * (v + vmax) / (2 * vmax)
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+14}" font-size="9" fill="{SUB}" text-anchor="middle">{v*100:+.2f}%</text>')
    bh = ch / (len(rules) + 0.5)
    for i, (name, c) in enumerate(rules):
        yy = y0 + bh * (i + 0.5)
        col = RED if c["mean"] > 0 else GREEN
        p.append(f'<text x="{x0-8}" y="{yy+4:.0f}" font-size="10" fill="{INK}" text-anchor="end">{name}</text>')
        pts = []
        for k, (val, lbl) in enumerate([(c["mean"], "发现"), (c["v2025"]["mean"] if c["v2025"] else None, "2025"), (c["v2026"]["mean"] if c["v2026"] else None, "2026")]):
            if val is None: continue
            xx = x0 + cw * (val + vmax) / (2 * vmax)
            pts.append((xx, k))
            r = 5 if k == 2 else 3.6
            p.append(f'<circle cx="{xx:.0f}" cy="{yy:.0f}" r="{r}" fill="{col}" fill-opacity="{1 if k==2 else 0.55}"/>')
        if len(pts) > 1:
            p.append('<polyline points="' + " ".join(f"{a:.0f},{yy:.0f}" for a, _ in pts) + f'" fill="none" stroke="{col}" stroke-width="1.6" stroke-opacity="0.5"/>')
        v26 = c["v2026"]["mean"] if c["v2026"] else None
        if v26 is not None:
            p.append(f'<text x="{x0+cw*(v26+vmax)/(2*vmax)+10:.0f}" y="{yy+4:.0f}" font-size="9.5" font-weight="700" fill="{col}">{v26:+.4f}%</text>')
    for k, lbl in enumerate(["发现2020-24", "2025", "2026 holdout"]):
        p.append(f'<text x="{x0+cw*0.06:.0f}" y="{y0-14+k*0:.0f}" font-size="9" fill="{SUB}"></text>')
    p.append(f'<text x="{x0+cw/2}" y="{y0+ch+30}" font-size="10" fill="{SUB}" text-anchor="middle">每行三个点=同一规则在三个时期的次日均值；最右大点=2026 holdout。全部同号，但 holdout 期 t 值显著衰减（样本仅105天且为强趋势行情）</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图5: 经济性 ============
def fig_econ():
    ma = QV["econ"]["MA"]["locked"]; eg = QV["econ"]["EG"]["locked"]
    items = []
    for c in ma:
        if c.get("edge26") is not None:
            items.append((c["rule"].replace("(纯形态基线)", ""), c["edge26"], "MA"))
    for c in eg:
        if c.get("edge26") is not None:
            items.append((c["rule"].replace("(纯形态基线)", ""), c["edge26"], "EG"))
    W, H = 680, 300; x0, y0, cw, ch = 200, 46, 440, 210
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">2026 holdout 经济性：信号收益 vs 往返成本（20元/手，含手续费+1跳滑点）</text>')
    vmax = 10
    for g in range(9):
        v = -vmax + 2 * vmax * g / 8
        xx = x0 + cw * (v + vmax) / (2 * vmax)
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+14}" font-size="9" fill="{SUB}" text-anchor="middle">{v:+.0f}</text>')
    # 成本带
    for cv, lab, dash in [(20, "+成本20", ""), (-20, "-成本20", "")]:
        xx = x0 + cw * (cv + vmax) / (2 * vmax)
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#c0392b" stroke-width="1.6" stroke-dasharray="5,3"/>')
    p.append(f'<text x="{x0+cw*(20+vmax)/(2*vmax)+4:.0f}" y="{y0+12}" font-size="9.5" font-weight="700" fill="#c0392b">+20元/手成本线</text>')
    p.append(f'<text x="{x0+cw*(-20+vmax)/(2*vmax)-4:.0f}" y="{y0+12}" font-size="9.5" font-weight="700" fill="#c0392b" text-anchor="end">-20元/手成本线</text>')
    bh = ch / len(items)
    for i, (name, e, sym) in enumerate(items):
        yy = y0 + bh * (i + 0.5)
        col = RED if e > 0 else GREEN
        xx0 = x0 + cw * (0 + vmax) / (2 * vmax); xx1 = x0 + cw * (e + vmax) / (2 * vmax)
        p.append(f'<text x="{x0-8}" y="{yy+4:.0f}" font-size="9.5" fill="{INK}" text-anchor="end">{sym} {name}</text>')
        p.append(f'<rect x="{min(xx0,xx1):.0f}" y="{yy-5:.0f}" width="{abs(xx1-xx0):.0f}" height="10" rx="2" fill="{col}" fill-opacity="0.8"/>')
        p.append(f'<text x="{xx1 + (6 if e>0 else -6):.0f}" y="{yy+4:.0f}" font-size="9.5" font-weight="700" fill="{col}" text-anchor="{"start" if e>0 else "end"}">{e:+.1f}</text>')
    p.append(f'<text x="{x0+cw/2}" y="{y0+ch+30}" font-size="10" fill="{SUB}" text-anchor="middle">元/手（乘数10吨）。所有规则收益都在成本线以内——量价复合提升了信号强度但仍未跨过交易门槛</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 表格 ============
# T1 任务① 单特征
t1 = []
for feat, fname in [("body_up", "实体方向（阳=1）"), ("has_upper", "上影有无"), ("has_lower", "下影有无")]:
    a = T12["features"]["MA"][f"{feat}_disc_2020_2025"]; b = T12["features"]["MA"][f"{feat}_holdout_2026"]
    t1.append([fname, f"{a['on']['mean']:+.4f}%", f"{a['off']['mean']:+.4f}%", f"{a['diff']:+.4f}%",
               f"{T12['features']['MA'][feat+'_r2_disc_2020_2025']:.4f}", f"{b['diff']:+.4f}%", f"{T12['features']['MA'][feat+'_r2_holdout_2026']:.4f}"])
T1 = fmt_tbl(["二值特征", "E[next|开]", "E[next|关]", "差值", "R²(发现期)", "差值(26)", "R²(26)"], t1)

# T2 任务① 组合
ma = T12["features"]["MA"]
t2 = [
    ["影线组合 4态", f"{ma['r2_shadow4_disc_2020_2025']:.4f}", f"{ma['r2_shadow4_holdout_2026']:.4f}"],
    ["实体 2态", f"{ma['r2_body2_disc_2020_2025']:.4f}", f"{ma['r2_body2_holdout_2026']:.4f}"],
    ["CLV 20箱", f"{ma['r2_clv20_disc_2020_2025']:.4f}", f"{ma['r2_clv20_holdout_2026']:.4f}"],
    ["CLV × 实体", f"{ma['r2_clv_x_body_disc_2020_2025']:.4f}", f"{ma['r2_clv_x_body_holdout_2026']:.4f}"],
    ["形态码 8态", f"{ma['r2_code8_disc_2020_2025']:.4f}", f"{ma['r2_code8_holdout_2026']:.4f}"],
]
T2 = fmt_tbl(["特征集", "R²(发现期2020-25)", "R²(2026 holdout)"], t2)

# T3 任务② regime 表
t3 = []
for s in STATES:
    a = T12["regime"]["MA"]["amp_disc_2020_2025"][s]; b = T12["regime"]["MA"]["amp_holdout_2026"][s]
    rg = T12["regime"]["MA"]["regime_map"][s]
    t3.append([f"<code>{s}</code>", f"{a['mean_amp']:.3f}%", f"{b['mean_amp']:.3f}%", rg])
T3 = fmt_tbl(["形态码", "E[下一根振幅] 发现期", "E[下一根振幅] 2026", "regime"], t3)

# T4 任务② regime 过滤无效
t4 = []
for s in CORE:
    rows = T12["regime"]["MA"]["sig_by_preregime"][s]
    cells = []
    for rg in ["低波", "中波", "高波"]:
        v = rows.get(f"{rg}_disc_2020_2025")
        cells.append(f"{rg} {v['mean']:+.4f}%(t{v['t']:+.0f})" if v else f"{rg} —")
    t4.append([f"<code>{s}</code>"] + cells)
T4 = fmt_tbl(["方向信号", "低波regime", "中波regime", "高波regime"], t4)

# T5 任务③ 量比扫描
t5 = []
bins = ["Q1最小", "Q2", "Q3", "Q4", "Q5最大"]
for s in STATES:
    row = [f"<code>{s}</code>"]
    for bn in bins:
        v = QV["scan"]["MA"][s].get(bn)
        row.append(cell(v["mean"], v["t"], v["n"]) if v else "—")
    t5.append(row)
T5 = fmt_tbl(["形态"] + [f"量比{bn}" for bn in bins], t5)

# T6 任务③ 锁定规则三级
t6 = []
for sym in ["MA", "EG"]:
    for c in QV["econ"][sym]["locked"]:
        if c["type"] == "base": continue
        v25, v26 = c.get("v2025"), c.get("v2026")
        t6.append([f"<code>{c['rule']}</code>", sym,
                   f"{c['mean']:+.4f}%<span class='tt'>t={c['t']:+.1f} n={c['n']:,}</span>",
                   cell(v25["mean"], v25["t"], v25["n"]) if v25 else "—",
                   cell(v26["mean"], v26["t"], v26["n"]) if v26 else "—",
                   f"{c['edge26']:+.1f}元/手" if c.get("edge26") is not None else "—"])
T6 = fmt_tbl(["复合规则", "品种", "发现期2020-24", "2025", "2026 holdout", "26收益/手"], t6)

# T7 基线对照
t7 = []
for sym in ["MA", "EG"]:
    for c in QV["econ"][sym]["locked"]:
        if c["type"] != "base": continue
        v26 = c.get("v2026")
        t7.append([sym, f"<code>{c['state']}</code>", cell(c["mean"], c["t"], c["n"]),
                   cell(v26["mean"], v26["t"], v26["n"]) if v26 else "—",
                   f"{c['edge26']:+.1f}元/手" if c.get("edge26") is not None else "—"])
T7 = fmt_tbl(["品种", "形态", "发现期", "2026 holdout", "26收益/手"], t7)

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>K线六位编码 · 三项任务与2026验证报告 V4</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:{INK};margin:0;padding:28px;line-height:1.65}}
.wrap{{max-width:980px;margin:0 auto;background:#fff;border-radius:14px;padding:36px 42px;box-shadow:0 2px 18px rgba(20,30,50,.07)}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:19px;margin:34px 0 10px;padding-bottom:7px;border-bottom:2px solid #eef1f5}}
h3{{font-size:15.5px;margin:22px 0 8px;color:#3a4a5e}}
.sub{{color:{SUB};font-size:12.5px;margin-bottom:18px}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:12px}}
th{{background:#f2f4f8;padding:6px 8px;text-align:left;border:1px solid #e3e7ee;font-size:11.5px;color:#4a5568}}
td{{padding:5px 8px;border:1px solid #e8ecf2}}
code{{font-family:Consolas,monospace;background:#f0f3f7;padding:1px 4px;border-radius:4px;font-size:11px}}
.tt{{display:block;font-size:9.5px;font-weight:400;color:{SUB}}} .nn{{display:block;font-size:9.5px;font-weight:400;color:#aab2c0}}
.box{{background:#f8fafc;border:1px solid #e6eaf1;border-radius:10px;padding:14px 18px;margin:14px 0;font-size:13.5px}}
.warn{{background:#fff8f0;border:1px solid #f5e0c0}} .ok{{background:#f2faf6;border:1px solid #cdebd9}} .bad{{background:#fdeeee;border:1px solid #f0c8c8}}
.fig{{margin:16px 0;border:1px solid #edf0f5;border-radius:10px;padding:10px;background:#fff}}
.fig svg{{width:100%;height:auto;display:block}}
ul{{margin:8px 0;padding-left:22px}} li{{margin:4px 0;font-size:13.5px}}
.pill{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:11.5px;font-weight:600;margin-right:6px}}
.p-pass{{background:#e4f6ee;color:#00875a}} .p-fail{{background:#fdeaea;color:#c0392b}} .p-weak{{background:#fff4dd;color:#a07400}}
.mut{{color:#b6bdc9}}
</style></head><body><div class="wrap">

<h1>K线六位编码 · 三项任务与 2026 验证报告（V4）</h1>
<div class="sub">甲醇 MA / 乙二醇 EG · 1分钟K线 · 任务①②③ + 严格三级协议：发现期 2020-2024 → 中间验证 2025 → 一次性 holdout 2026（105个交易日，35,865根/品种，价格 +59%~+65% 强趋势行情）</div>

<div class="box warn"><b>协议说明</b>：所有规律只在<b>发现期 2020-2024</b> 扫描与筛选；2025 作为中间验证；<b>2026 为一次性 holdout</b>——规则锁定后只看一次结果，不回头改规则。2026 的考验比普通样本外更严苛：数据截止 6 月 12 日、仅 105 天、且甲醇从 2177 涨到 3595（+65%）、乙二醇 3657→5807（+59%），而发现期大半是震荡/熊市——<b>规律必须在完全不同的市场状态下仍然成立才算存活</b>。</div>

<h2>任务① 影线位置作为独立二值特征 vs 实体方向</h2>
<p>把「上影有无」「下影有无」拆为独立二值特征，与实体方向（阴阳）做预测力对比。用户命题是"影线位置比实体方向更有效"——检验结果：</p>
{T1}
<div class="fig">{fig_r2()}</div>
{T2}
<div class="box bad"><b>命题的字面版本不成立，机制版本成立。</b>①单特征对比：实体方向的 R²（0.0101）<b>高于</b>单个影线特征（上影 0.0035 / 下影 0.0038）——"影线比实体更有效"在单特征口径下是错的；②但实体方向的预测力主要是<b>收盘位置（CLV）的代理</b>：CLV20箱单独就有 R²=0.0248，超过任何二值特征与形态码本身（0.0228），而"CLV×实体"仅比 CLV 单独高 0.0003——控制 CLV 后实体方向几乎无增量；③准确的表述：<b>影线位置（通过共同决定 CLV）比实体方向更有效；实体方向的表面预测力是"收阳/收阴"与"收盘位置"的伴生现象</b>；④2026 holdout：所有特征 R² 塌缩 60-75%（0.0248→0.0067），形态类特征在强趋势行情中预测力大幅衰减——这是本次验证最重要的边界条件。</div>

<h2>任务② 用形态码做波动率 regime 过滤</h2>
<p>先检验形态码对<b>下一根振幅</b>的预测（regime 表本身），再检验用该 regime 过滤方向信号能否提升信噪比：</p>
<div class="fig">{fig_regime()}</div>
{T3}
<div class="box ok"><b>regime 表跨期完全稳定。</b>八态按 E[下一根振幅] 的排序在发现期与 2026 holdout 上<b>逐位一致</b>：无影光头（010000/000000）→ 低波态（0.10%/0.11%），双影阴（000101）→ 高波态（0.16%/0.21%），乙二醇同样保持排序（整体上移约40%，因 2026 波动环境更高）。<b>形态码确实是可靠的波动率状态指示器</b>——这正是你原方案中"线段部分是对振幅做处理"的量化印证。</div>
{T4}
<div class="box bad"><b>但用 regime 过滤方向信号没有增益（诚实负面结果）。</b>四个核心方向信号在低/中/高波三档 regime 内的效应几乎恒定（010000：-0.0180/-0.0216/-0.0216；000100：+0.0200/+0.0203/+0.0209）——<b>信号效应不随波动状态调制</b>。regime 的价值在于"知道下一根大概多大"（可用于仓位与止损设置），而不在于"决定何时用方向信号"。</div>

<h2>任务③ 与量价信息复合：发现 → 2026 一次性验证</h2>
<p>量价特征全部只用 t 时刻可知信息：量比（vol/前20分钟均量）、量能分位、OI 分钟变化。扫描 <b>40 个形态×量比单元 + 放量/缩量/OI 增减交互共 77 个候选</b>，发现期 |t|&gt;5 且 n&gt;500 的入选 43 条（MA）/34 条（EG），按族取最强锁定：</p>
<div class="fig">{fig_volscan()}</div>
{T5}
<div class="box"><b>扫描的第一结论：1 分钟形态效应"不挑量"。</b>四类核心信号在五个量比档下的效应几乎恒定（000000 五档 +0.0213~+0.0237；010000 五档 -0.0210~-0.0201）——放量/缩量不改变次日方向效应，OI 增减同样无调制（010001×OI增 -0.0193 vs ×OI减 -0.0184）。<b>唯一有调制的是 010100（双影阳）：Q1 +0.0120% → Q5 -0.0027%</b>（放量的双影阳线次日反转为跌），EG 同向。</div>
<div class="fig">{fig_3tier()}</div>
{T6}
{T7}
<div class="fig">{fig_econ()}</div>
<div class="box bad"><b>holdout 判定：三条锁定结论。</b>①<b>所有规则方向全部一致</b>——发现→2025→2026 三级同号（MA 000000×缩量：+0.0214 → +0.0208 → +0.0210；EG 同），规律本身在强趋势行情中存活；②但 <b>010100×放量的调制未通过 holdout</b>（MA：发现期放量 -0.0048 vs 非放量 +0.0043，2026 翻转为 +0.0032 vs +0.0061）——这是发现期过拟合，应弃用；③<b>经济性仍不过关</b>：2026 holdout 上最强信号为 EG 000000×缩量 +7.9 元/手、MA 000000×缩量 +5.7 元/手，三元规则 010000×放量×OI增 在 2026 达 -7.7 元/手（是基线的 2 倍，但仅 n=58），全部低于往返成本 20 元/手（手续费约 6 + 一跳滑点约 14）。<b>1 分钟级别不存在可交易的量价复合信号</b>。</div>

<h2>总结论</h2>
<div class="box ok">
<b>对三项任务的最终回答</b>：<br><br>
① <b>影线 vs 实体</b>：单特征口径下实体方向 R² 更高，但其效果主要是 CLV 的伴生；影线位置通过决定 CLV 成为主驱动（R² 0.0248 vs 实体 0.0101）。你的六位编码中，<b>真正有效的信息依次是：收盘位置（影线共同决定）&gt; 实体方向 &gt; 影线单独字段</b>。编码方案本身有增量（控制 CLV 后 F=53.7），成立。<br><br>
② <b>波动率 regime</b>：形态码 → 下一根振幅的表跨期逐位稳定（八态排序 2020-2025 与 2026 完全一致），是可靠的波动率状态指示器，可用于仓位/止损标定；但<b>不能用于过滤方向信号</b>——信号效应在各 regime 档内恒定。<br><br>
③ <b>量价复合</b>：77 个候选中，绝大多数无增益（效应不挑量、不挑 OI）；唯一发现期显著的 010100×放量调制被 2026 holdout 证伪；三元规则（形态×放量×OI增）能把信号强度提升约 1 倍（MA 010000：基线 -3.9 → 复合 -7.7 元/手）但样本稀少（n=58/年）。<b>结论：所有规则在 2026 同向存活，但无一跨过 20 元/手的成本线——1 分钟形态+量价的正确用法是特征工程（波动率状态、执行风控、仓位标定），不是独立交易信号。</b><br><br>
<span class="pill p-pass">存活</span>形态→振幅 regime 表（跨期逐位稳定）　<span class="pill p-pass">存活</span>四核心方向信号（2026 全部同向）　<span class="pill p-fail">证伪</span>010100×放量调制（holdout 翻转）　<span class="pill p-fail">证伪</span>regime 过滤提升信噪比（无调制）　<span class="pill p-fail">证伪</span>1分钟可交易性（信号&lt;成本）
</div>

<div class="sub" style="margin-top:26px;border-top:1px solid #eef1f5;padding-top:12px">
数据：D:\\学习资料 CZCE/DCE 主力连续 1 分钟（KQ.m@），2020-2026 全样本（2026 至 6/12 截止，105 天）。脚本：step13_extract_2026.py / step14_task12.py / step15_qv.py；结果：results_task12.json / results_qv.json。相邻口径：gap==60s 严格相邻；t 值为均值检验；经济性按乘数 10 吨/手、成本 20 元/手（手续费双边 6 + 一跳滑点 14）估算。
</div>
</div></body></html>"""

open(f"{OUT}/kline_pattern_report_v4.html", "w", encoding="utf-8").write(html)
print("report_v4 saved, chars:", len(html))
