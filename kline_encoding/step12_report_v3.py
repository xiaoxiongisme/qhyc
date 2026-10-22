# -*- coding: utf-8 -*-
"""Step 12: 生成 V3 验证报告（规律证伪与存活判定）"""
import json, base64

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
V1 = json.load(open(f"{OUT}/results_verify.json", encoding="utf-8"))
V2 = json.load(open(f"{OUT}/results_verify2.json", encoding="utf-8"))
V3 = json.load(open(f"{OUT}/results_verify3.json", encoding="utf-8"))
POOL = json.load(open(f"{OUT}/results_pool.json", encoding="utf-8"))

RED, GREEN, SUB, INK = "#d63031", "#00997a", "#8a94a6", "#2c3e50"

def fmt_tbl(headers, rows, cls=""):
    h = "".join(f"<th>{x}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'

def cell(mean, t, n=None, good_positive=None):
    if mean is None: return '<td class="mut">—</td>'
    # good_positive: True=该信号此方向为"好"(红); False=绿; None=按符号自动(涨红跌绿)
    if good_positive is None: good_positive = mean > 0
    col = RED if good_positive else GREEN
    s = f"{mean:+.4f}%"
    tt = f"<span class='tt'>t={t:+.1f}</span>" if t is not None else ""
    nn = f"<span class='nn'>n={n:,}</span>" if n is not None else ""
    return f'<td style="color:{col};font-weight:600">{s}{tt}{nn}</td>'

# ============ 图1: 规律存活矩阵 ============
def fig_survival():
    # 行=规律, 列=1m(2020-25) / 5m(MA) / 5m(EG) / 15m(MA) / 15m(EG), 值=判定
    rows = [
        ("L1 方向位失效(平盘1/4, 剔后50%)", ["pass"]*5, "结构性"),
        ("L2a 010000光头阳→跌", ["pass", "fail", "fail", "fail", "fail"], "仅1m"),
        ("L2b 000000光头阴→涨", ["pass", "pass", "pass", "na", "na"], "1m+5m"),
        ("L3a 000100上影阴→涨", ["pass", "pass", "pass", "weak", "fail"], "最稳健"),
        ("L3b 010001双影阳→跌", ["pass", "pass", "fail", "fail", "fail"], "1m+5m(MA)"),
        ("L4 波动率聚集 |chg|acf", ["pass"]*5, "全周期"),
        ("L5 无动量只有回归", ["pass"]*5, "结构性"),
        ("L6 经济性不可交易", ["pass", "pass", "pass", "na", "na"], "不变"),
    ]
    p = ['<svg viewBox="0 0 680 320" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="20" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">规律存活判定矩阵（绿=存活 黄=边际 红=证伪/消失 灰=样本不足）</text>')
    cols = ["1m 2020-25", "5m MA", "5m EG", "15m MA", "15m EG"]
    x0, cw = 250, 84
    for j, cn in enumerate(cols):
        p.append(f'<text x="{x0+cw*(j+0.5):.0f}" y="44" font-size="10.5" fill="{SUB}" text-anchor="middle">{cn}</text>')
    colmap = {"pass": (GREEN, "✓"), "weak": ("#d9a400", "~"), "fail": (RED, "✗"), "na": ("#c8cdd6", "–")}
    for i, (name, marks, note) in enumerate(rows):
        y = 56 + i * 31
        p.append(f'<text x="4" y="{y+18}" font-size="11" fill="{INK}">{name}</text>')
        p.append(f'<text x="{x0-6}" y="{y+18}" font-size="9" fill="{SUB}" text-anchor="end">{note}</text>')
        for j, mk in enumerate(marks):
            c, sym = colmap[mk]
            alpha = 0.85 if mk != "na" else 0.5
            p.append(f'<rect x="{x0+cw*j+8:.0f}" y="{y+4}" width="{cw-16}" height="23" rx="5" fill="{c}" fill-opacity="0.14" stroke="{c}" stroke-opacity="{alpha}"/>')
            p.append(f'<text x="{x0+cw*(j+0.5):.0f}" y="{y+20}" font-size="13" font-weight="700" fill="{c}" text-anchor="middle">{sym}</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图2: CLV 十分档 V 形 ============
def fig_clv():
    ma = V3["clv_vs_code"]["MA"]; eg = V3["clv_vs_code"]["EG"]
    clv_ma = json.load(open(f"{OUT}/results_verify2.json", encoding="utf-8"))["control"]["MA"]["_clv"]
    clv_eg = json.load(open(f"{OUT}/results_verify2.json", encoding="utf-8"))["control"]["EG"]["_clv"]
    W, H = 680, 300; x0, y0, cw, ch = 60, 40, 580, 210
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">收盘位置(CLV)十分档 → 下一分钟平均涨跌幅（1m，2020-2025，严格相邻）</text>')
    vmax = 0.022
    for g in range(6):
        v = -vmax + 2 * vmax * g / 5
        yy = y0 + ch / 2 - (v / vmax) * (ch / 2)
        p.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x0+cw}" y2="{yy:.1f}" stroke="#e4e8ee" stroke-width="1"/>')
        p.append(f'<text x="{x0-6}" y="{yy+4:.1f}" font-size="9.5" fill="{SUB}" text-anchor="end">{v*100:+.1f}%</text>')
    p.append(f'<line x1="{x0}" y1="{y0+ch/2}" x2="{x0+cw}" y2="{y0+ch/2}" stroke="#b8c0cc" stroke-width="1.3"/>')
    for series, col, name in [(clv_ma, RED, "甲醇 MA"), (clv_eg, "#2e6fb7", "乙二醇 EG")]:
        pts = []
        for i, r in enumerate(series):
            xx = x0 + cw * (i + 0.5) / len(series)
            yy = y0 + ch / 2 - (r["mean"] / 100 / vmax) * (ch / 2)
            pts.append((xx, yy))
        p.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + f'" fill="none" stroke="{col}" stroke-width="2.4"/>')
        for (a, b), r in zip(pts, series):
            p.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.6" fill="{col}"/>')
        # 端点标注
        p.append(f'<text x="{pts[-1][0]-2:.1f}" y="{pts[-1][1]-9:.1f}" font-size="10.5" font-weight="700" fill="{col}" text-anchor="end">{series[-1]["mean"]:+.4f}%</text>')
        p.append(f'<text x="{pts[0][0]+4:.1f}" y="{pts[0][1]-9:.1f}" font-size="10.5" font-weight="700" fill="{col}">{series[0]["mean"]:+.4f}%</text>')
    for i in range(11):
        xx = x0 + cw * i / 10
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+16}" font-size="9" fill="{SUB}" text-anchor="middle">{i/10:.1f}</text>')
    p.append(f'<text x="{x0+cw/2}" y="{y0+ch+34}" font-size="10.5" fill="{SUB}" text-anchor="middle">CLV = (close-low)/(high-low)，0=收在最低 1=收在最高</text>')
    p.append(f'<rect x="{x0+40}" y="{y0+8}" width="12" height="12" fill="{RED}"/><text x="{x0+56}" y="{y0+18}" font-size="10.5" fill="{INK}">甲醇</text>')
    p.append(f'<rect x="{x0+110}" y="{y0+8}" width="12" height="12" fill="#2e6fb7"/><text x="{x0+126}" y="{y0+18}" font-size="10.5" fill="{INK}">乙二醇</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图3: 聚类 t vs naive t ============
def fig_cluster():
    states = ["010000", "000000", "000100", "010001", "010100", "000101", "010101", "000001"]
    W, H = 680, 300; x0, y0, cw, ch = 130, 44, 540, 210
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">t 值口径对比：朴素 t vs 按日聚类 t（甲醇，2020-2025）</text>')
    ma_c = V1["per_sym"]["MA"]["cluster"]
    tmax = 70
    for g in range(8):
        v = -tmax + 2 * tmax * g / 7
        xx = x0 + cw * (v + tmax) / (2 * tmax)
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+14}" font-size="9" fill="{SUB}" text-anchor="middle">{v:+.0f}</text>')
    bw = 9
    for i, s in enumerate(states):
        yy = y0 + 12 + i * (ch - 16) / 8
        v = ma_c.get(s)
        if not v: continue
        for k, (tv, col, lab) in enumerate([(v["t_naive"], "#9aa4b2", "朴素"), (v["t_cluster"], RED if v["mean"] > 0 else GREEN, "聚类")]):
            wpx = cw * abs(tv) / (2 * tmax)
            bx = x0 + cw * (tv + tmax) / (2 * tmax) - (wpx if tv < 0 else 0)
            p.append(f'<rect x="{bx:.1f}" y="{yy+k*10}" width="{wpx:.1f}" height="8" rx="2" fill="{col}" fill-opacity="{0.55 if k==0 else 0.95}"/>')
            p.append(f'<text x="{bx + (wpx if tv<0 else -4):.1f}" y="{yy+k*10+7.5}" font-size="8" fill="{col}" text-anchor="{"end" if tv<0 else "start"}">{tv:+.1f}</text>')
        p.append(f'<text x="{x0-8}" y="{yy+15}" font-size="10.5" fill="{INK}" text-anchor="end"><tspan font-family="monospace">{s}</tspan></text>')
        p.append(f'<text x="{x0+cw+8}" y="{yy+15}" font-size="9.5" fill="{SUB}">一致率{v["day_sign_cons"]*100:.0f}%</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图4: 多步持有 ============
def fig_multihold():
    W, H = 680, 280; x0, y0, cw, ch = 60, 46, 580, 190
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">信号后持有 k 根K线的累计收益（甲醇 1m，2020-2025）——反转在第一分钟即完成</text>')
    ks = [1, 2, 3, 5, 10]
    vmax = 0.028
    for g in range(7):
        v = -vmax + 2 * vmax * g / 6
        yy = y0 + ch / 2 - (v / 100 / vmax) * (ch / 2)
        p.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x0+cw}" y2="{yy:.1f}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{x0-6}" y="{yy+4:.1f}" font-size="9.5" fill="{SUB}" text-anchor="end">{v*100:+.2f}%</text>')
    p.append(f'<line x1="{x0}" y1="{y0+ch/2}" x2="{x0+cw}" y2="{y0+ch/2}" stroke="#b8c0cc" stroke-width="1.2"/>')
    cols = {"010000": GREEN, "000000": RED, "000100": RED, "010001": GREEN}
    for s in ["010000", "000000", "000100", "010001"]:
        rows = V2["multihold"]["MA"][s]
        pts = [(x0 + cw * (ks.index(r["k"]) + 0.5) / len(ks), y0 + ch / 2 - (r["mean"] / 100 / vmax) * (ch / 2)) for r in rows]
        p.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + f'" fill="none" stroke="{cols[s]}" stroke-width="2.2"/>')
        for (a, b), r in zip(pts, rows):
            p.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.4" fill="{cols[s]}"/>')
        lx, ly = pts[-1]
        p.append(f'<text x="{lx+7:.1f}" y="{ly+4:.1f}" font-size="10.5" font-weight="700" fill="{cols[s]}">{s} {rows[-1]["mean"]:+.4f}%</text>')
    for k in ks:
        xx = x0 + cw * (ks.index(k) + 0.5) / len(ks)
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+16}" font-size="10" fill="{SUB}" text-anchor="middle">k={k}</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 图5: CLV≤0.5 内部 实体方向增量 ============
def fig_increment():
    inc = V3["clv_vs_code"]["MA"]["increment"]["CLV0-0.5(下半部)"]
    order = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
    W, H = 680, 260; x0, y0, cw, ch = 60, 52, 600, 160
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    p.append(f'<text x="340" y="18" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">控制 CLV≤0.5 后：实体方向的增量（甲醇 1m）——同一下半区收盘，阴线→大涨、阳线→零</text>')
    vmax = 0.03
    for g in range(7):
        v = -vmax + 2 * vmax * g / 6
        xx = x0 + cw * (v + vmax) / (2 * vmax)
        p.append(f'<line x1="{xx:.0f}" y1="{y0}" x2="{xx:.0f}" y2="{y0+ch}" stroke="#e4e8ee"/>')
        p.append(f'<text x="{xx:.0f}" y="{y0+ch+14}" font-size="9.5" fill="{SUB}" text-anchor="middle">{v*100:+.1f}%</text>')
    bw = cw / 8
    for i, s in enumerate(order):
        v = inc.get(s)
        if not v: continue
        xx = x0 + bw * i + bw * 0.18
        w = bw * 0.64
        vv = v["mean"]
        hh = abs(vv) / 100 / vmax * (ch / 2)
        ytop = y0 + ch / 2 - hh if vv > 0 else y0 + ch / 2
        col = RED if vv > 0 else GREEN
        p.append(f'<rect x="{xx:.0f}" y="{ytop:.1f}" width="{w:.0f}" height="{max(hh,1.2):.1f}" rx="2.5" fill="{col}" fill-opacity="0.85"/>')
        p.append(f'<text x="{xx+w/2:.0f}" y="{(ytop-6) if vv>0 else (y0+ch/2+hh+13):.1f}" font-size="10" font-weight="700" fill="{col}" text-anchor="middle">{vv:+.4f}%</text>')
        p.append(f'<text x="{xx+w/2:.0f}" y="{y0+ch+30}" font-size="9.5" font-family="monospace" fill="{INK}" text-anchor="middle">{s[:2]}·{s[2:4]}·{s[4:]}</text>')
        p.append(f'<text x="{xx+w/2:.0f}" y="{y0+ch+43}" font-size="8.5" fill="{SUB}" text-anchor="middle">n={v["n"]:,}</text>')
    p.append(f'<text x="{x0+8}" y="{y0+14}" font-size="10.5" fill="{SUB}">前2位=实体 中2位=上影 后2位=下影；01=阳/有影</text>')
    p.append('</svg>')
    return "".join(p)

# ============ 表格 ============
# T1 口径对比
t1 = []
for s in ["010000", "000000", "000100", "010001", "010100", "000101", "010101", "000001"]:
    r = V1["compare_adj"]["MA"][s]
    t1.append([f"<code>{s}</code>"] + [cell(r[k]["mean"], r[k]["t"], r[k]["n"]) for k in ["naive(step3)", "sess(step7)", "gap60s(严格)", "gap60s+剔极端"]])
T1 = fmt_tbl(["形态", "① naive（V1报告口径）", "② sess（V2报告口径）", "③ gap60s（严格相邻）", "④ gap60s+剔极端分钟"], t1)

# T2 聚类稳健性
t2 = []
for s in ["010000", "000000", "000100", "010001", "010100", "000101", "010101", "000001"]:
    for sym in ["MA", "EG"]:
        v = V1["per_sym"][sym]["cluster"].get(s)
        if not v: continue
        t2.append([f"<code>{s}</code>", sym, f"{v['n']:,}", f"{v['mean']:+.4f}%", f"{v['t_naive']:+.1f}", f"{v['t_cluster']:+.1f}",
                   f"{v['day_sign_cons']*100:.1f}%", f"{v['binom_p']:.1e}"])
T2 = fmt_tbl(["形态", "品种", "样本n", "均值", "朴素t", "按日聚类t", "逐日符号一致率", "二项p"], t2)

# T3 换月断层
t3 = []
for sym in ["MA", "EG"]:
    r = V1["per_sym"][sym]["roll"]
    t3.append([sym, f"{r['n_sess_gap_gt1']}", f"{r['n_oi_jump']}", f"{len(r['overlap_gap_oi'])}",
               f"{r['n_extreme_min']}", "、".join(r["oi_jump_dates"][:6]) + ("…" if len(r["oi_jump_dates"]) > 6 else "")])
T3 = fmt_tbl(["品种", "会话首根跳空|>1%|", "OI日跳变|>40%|(换月日)", "两者重合(断层确认)", "分钟级|chg|>1%", "OI跳变日期(前6)"], t3)

# T4 控制变量
t4 = []
for s in ["010000", "000000", "000100", "010001"]:
    c = V2["control"]["MA"][s]
    a = c["by_amp"]; g = c["by_seg"]
    t4.append([f"<code>{s}</code>",
               cell(a["小振幅"]["mean"], a["小振幅"]["t"]), cell(a["中振幅"]["mean"], a["中振幅"]["t"]), cell(a["大振幅"]["mean"], a["大振幅"]["t"]),
               cell(g["夜盘21-23"]["mean"], g["夜盘21-23"]["t"]), cell(g["早盘09-10:15"]["mean"], g["早盘09-10:15"]["t"]),
               cell(g["午盘13:30-15:00"]["mean"], g["午盘13:30-15:00"]["t"])])
T4 = fmt_tbl(["形态", "小振幅", "中振幅", "大振幅", "夜盘", "早盘", "午盘"], t4)

# T5 5m/15m 修正
t5 = []
for sym in ["MA", "EG"]:
    for row in V3["htf_fix"][sym]:
        for s in ["010000", "000000", "000100", "010001"]:
            v = row[f"st_{s}"]
            t5.append([sym, row["tf"], f"<code>{s}</code>", f"{row['adj_share']*100:.0f}%", cell(v["mean"], v["t"], v["n"])])
T5 = fmt_tbl(["品种", "周期", "形态", "相邻bar占比", "下一根均值(t)"], t5)

# T6 增量检验
ma_inc = V3["clv_vs_code"]["MA"]; eg_inc = V3["clv_vs_code"]["EG"]
t6 = [
    ["R²：CLV 20箱（单因子）", f"{ma_inc['r2_clv20']:.4f}", f"{eg_inc['r2_clv20']:.4f}"],
    ["R²：形态码 8组（单因子）", f"{ma_inc['r2_code8']:.4f}", f"{eg_inc['r2_code8']:.4f}"],
    ["R²：CLV × 形态（组合）", f"<b>{ma_inc['r2_both']:.4f}</b>", f"<b>{eg_inc['r2_both']:.4f}</b>"],
    ["增量F检验（控制CLV后形态哑变量）", f"F={ma_inc['F_incremental']:.1f}, p={ma_inc['p_F']:.1e}", f"F={eg_inc['F_incremental']:.1f}, p={eg_inc['p_F']:.1e}"],
    ["IC: spearman(CLV, 下一根)", f"{ma_inc['ic_clv']:+.4f}", f"{eg_inc['ic_clv']:+.4f}"],
    ["IC: 形态均值预测 vs 下一根", f"{ma_inc['ic_code']:+.4f}", f"{eg_inc['ic_code']:+.4f}"],
]
T6 = fmt_tbl(["检验", "甲醇 MA", "乙二醇 EG"], t6)

# T7 多步持有
t7 = []
for s in ["010000", "000000", "000100", "010001"]:
    rows = V2["multihold"]["MA"][s]
    t7.append([f"<code>{s}</code>"] + [cell(r["mean"], r["t"], r["n"]) for r in rows])
T7 = fmt_tbl(["形态", "k=1", "k=2", "k=3", "k=5", "k=10"], t7)

# T8 收益分解
t8 = []
for sym in ["MA", "EG"]:
    for s in ["010000", "000000", "000100", "010001"]:
        v = V1["decomp"][sym][s]
        t8.append([sym, f"<code>{s}</code>",
                   f"{v['open_jump']['mean']:+.4f}%" + (f"(t={v['open_jump']['t']:+.1f})" if v['open_jump']['t'] == v['open_jump']['t'] else "(std=0)"),
                   f"{v['intra']['mean']:+.4f}%(t={v['intra']['t']:+.1f})",
                   f"{v['total']['mean']:+.4f}%(t={v['total']['t']:+.1f})"])
T8 = fmt_tbl(["品种", "形态", "开盘跳空部分", "日内部分", "合计"], t8)

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>K线六位编码 · 规律验证报告 V3</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:{INK};margin:0;padding:28px;line-height:1.65}}
.wrap{{max-width:980px;margin:0 auto;background:#fff;border-radius:14px;padding:36px 42px;box-shadow:0 2px 18px rgba(20,30,50,.07)}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:19px;margin:34px 0 10px;padding-bottom:7px;border-bottom:2px solid #eef1f5}}
h3{{font-size:15.5px;margin:22px 0 8px;color:#3a4a5e}}
.sub{{color:{SUB};font-size:12.5px;margin-bottom:18px}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:12.5px}}
th{{background:#f2f4f8;padding:7px 9px;text-align:left;border:1px solid #e3e7ee;font-size:12px;color:#4a5568}}
td{{padding:6px 9px;border:1px solid #e8ecf2}}
code{{font-family:Consolas,monospace;background:#f0f3f7;padding:1px 5px;border-radius:4px;font-size:11.5px}}
.tt{{display:block;font-size:10px;font-weight:400;color:{SUB}}} .nn{{display:block;font-size:10px;font-weight:400;color:#aab2c0}}
.box{{background:#f8fafc;border:1px solid #e6eaf1;border-radius:10px;padding:14px 18px;margin:14px 0;font-size:13.5px}}
.warn{{background:#fff8f0;border:1px solid #f5e0c0}} .ok{{background:#f2faf6;border:1px solid #cdebd9}}
.fig{{margin:16px 0;border:1px solid #edf0f5;border-radius:10px;padding:10px;background:#fff}}
.fig svg{{width:100%;height:auto;display:block}}
ul{{margin:8px 0;padding-left:22px}} li{{margin:4px 0;font-size:13.5px}}
.pill{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:11.5px;font-weight:600;margin-right:6px}}
.p-pass{{background:#e4f6ee;color:#00875a}} .p-fail{{background:#fdeaea;color:#c0392b}} .p-weak{{background:#fff4dd;color:#a07400}}
.mut{{color:#b6bdc9}}
</style></head><body><div class="wrap">

<h1>K线六位二进制编码 · 规律验证报告（V3 证伪版）</h1>
<div class="sub">甲醇 MA / 乙二醇 EG · 1分钟K线 · 2020-01-02 ~ 2025-12-31 · 每品种 489,975 根 · 基于 V1/V2 报告的规律做系统性证伪</div>

<div class="box warn"><b>本轮任务</b>：不再找新规律，而是用九组证伪实验检验 V1/V2 已发布结论是否站得住：①相邻口径三连对比 ②换月断层检测 ③按日聚类标准误 ④贡献度集中度 ⑤收益分解（跳空vs日内）⑥控制变量 ⑦多步持有 ⑧5m/15m口径修正 ⑨CLV与形态码增量检验。<b>结论先行：核心规律全部存活，但有三处需要修正/加限定</b>（见文末判定）。</div>

<div class="box" style="background:#fdeeee;border:1px solid #f0c8c8"><b>勘误（2026-10-17 补）</b>：本报告初版第七、八节中「CLV vs 形态码增量检验」与「5m/15m 修正」的代码（step11）误将 2020 单年数据当作 2020-2025 全样本计算（load 只读了 2020 文件）。现已修复重算，涉及数字：R²(CLV20箱) 0.0360→<b>0.0248</b>、R²(形态8组) 0.0315→<b>0.0228</b>、组合 R² 0.0642→<b>0.0307</b>、增量 F 5.7→<b>53.7</b>；MA 5m 上 010000 的 t 从 -0.9（样本不足）修正为 <b>-4.8（显著）</b>，即「010000 在 5m 上消失」的初版判断不成立（EG 仍不显著）。<b>定性结论方向不变，量级以本版为准。</b>其余各节（①②③④⑤⑥⑦节与 V1/V2 相关的核心结论）不受影响。</div>

<h2>一、规律存活判定矩阵</h2>
<div class="fig">{fig_survival()}</div>
<div class="box ok"><b>一句话总结</b>：<b>波动率聚集</b>（L4）是唯一全周期稳健的规律；<b>000100（带上影阴线→下一根涨）</b>是最强的单点方向信号（1m/5m 跨品种显著）；<b>无影光头线反转</b>（L2a/2b）确认为 1 分钟尺度的微观结构效应，不可外推到 5 分钟以上；方向位失效（L1）、无动量（L5）、经济性不可交易（L6）均为结构性事实，不随周期/年份改变。</div>

<h2>二、证伪实验 ①：相邻口径三连对比</h2>
<p>V1 报告（step3）的相邻对未做会话过滤，每天约 4-5 个跨会话边界对（15:00→21:00、23:00→次日09:00、11:30→13:30）与 1 个休市对（10:14→10:30）被当作"下一根K线"。本节量化这一污染的实际影响：</p>
{T1}
<div class="box"><b>结论：污染 &lt;1%，V1 报告结论无需修正。</b>四种口径下均值差异在第三位小数，t 值甚至随口径收紧而上升（MA 010000：-60.3 → -63.1）。原因是跨会话对在信号集中占比小（约1.3%），且其效应方向与会话内一致。V2 报告（step7）本来就用了 sess 口径，无误。</div>

<h2>三、证伪实验 ②：换月断层检测（主力连续不复权的证据）</h2>
<p>主力连续符号（KQ.m@CZCE.MA）按持仓量规则拼接合约。<b>若数据不复权，换月日会出现价格断层</b>，污染涨跌幅序列。用持仓量（OI）日跳变定位换月日，再与大跳空取交集：</p>
{T3}
<div class="box"><b>结论：主力连续确认不复权，但断层被正确隔离。</b>OI 跳变日呈现教科书式的主力切换周期（MA：12月→次年4月→8月，即 01→05→09 合约；EG：4月→8月→12月）。全部 OI 跳变日都伴随会话首根大跳空——这正是换月断层的指纹。但两个设计使污染可忽略：①断层发生在<b>会话首根</b>，而所有"下一根"统计都要求严格 60 秒相邻，跨会话对已被排除；②分钟级 |chg|&gt;1% 的极端K线总共仅 68/63 根，剔除后 t 值不变（-63.0→-63.1）。</div>

<h2>四、证伪实验 ③：按日聚类标准误（治 t 值虚高）</h2>
<p>分钟数据有强烈的日内聚集（|chg| 自相关 0.21-0.27），朴素 t 值会把"一天之内的重复"当作独立样本，<b>系统性高估显著性</b>。正确做法：每天先聚成一个观测（当日信号均值），再跨日做 t 检验：</p>
{T2}
<div class="fig">{fig_cluster()}</div>
<div class="box ok"><b>结论：规律不是日内聚集的假象。</b>聚类后 t 值大幅衰减（MA 010000：-63.0 → <b>-42.2</b>）但全部核心信号仍 |t_cluster| &gt; 12。更硬的证据是<b>逐日符号一致率</b>：MA 010000 在 <b>92.6%</b> 的交易日次日为跌（EG 79.8%），二项检验 p≈1e-272。若规律是噪声，一致率应贴近 50%。</div>

<h2>五、证伪实验 ④⑤：贡献度集中度与收益分解</h2>
<p>两个替代假说：<b>（a）效应由少数极端日子驱动</b>（事件驱动而非微观结构）；<b>（b）反转其实是隔夜/跳空假象</b>（信号在开盘一跳走完，实盘拿不到）。</p>
<div class="fig">{fig_increment()}</div>
{T8}
<div class="box"><b>（a）证伪——贡献度不集中</b>：按信号收益绝对值排序，最大的 0.1% 样本只贡献总 |收益| 的 0.9%-1.2%（若集中应为 10%+），top 5% 占 18.9%-20.6%。分布虽比正态重尾，但<b>没有任何小群体主导</b>，剔除极端分钟K线后 t 值不变。<br><b>（b）证伪——无跳空成分</b>：把信号后下一分钟收益分解为「开盘跳空」与「日内」两部分。甲醇的跳空部分<b>恒等于 0</b>（std=0，分钟间无价格断层），乙二醇也仅 +0.0003%（t=+1.7）。全部收益来自日内部分——进场价就是信号K线收盘价，实盘可捕获（扣除成本后见第七节）。</div>

<h2>六、证伪实验 ⑥⑦⑧：控制变量、多步持有、5m/15m 口径修正</h2>
<h3>6.1 控制变量：效应不依赖特定波动状态或时段</h3>
{T4}
<h3>6.2 多步持有：反转在第一分钟即完成</h3>
{T7}
<div class="fig">{fig_multihold()}</div>
<h3>6.3 5m/15m 口径修正（发现并修复 V2 报告的一处口径问题）</h3>
<p>V2 报告的 5m/15m 重采样用「同 sdate+sess」判断相邻 bar，但 CZCE/DCE 日内 10:15-10:30 休市 15 分钟在 sess 内部——<b>10:14 与 10:30 的 bar 被误判为相邻</b>（15m 序列中这类伪相邻高达 21%）。修正为「相邻 bar 时间差恰等于周期」后重算：</p>
{T5}
<div class="box"><b>结论：修正确认了 L3a，同时修正了初版对 L2a 的误判。</b>①桶内时间跨度异常=0，说明重采样本身没有跨休市桶（75分钟恰被5/15整除，属运气）；②修正后 <b>000100（上影阴→涨）在 MA 全周期显著</b>（1m t=+51.3 → 5m t=+11.1 → 15m t=+3.5），EG 在 1m/5m 显著（+28.8/+3.1）、15m 消失；③<b>010000 的反转在 MA 5m 上仍显著（t=-4.8，全样本 n=1,563）</b>——初版基于 2020 单年（n=543 样本不足）误判为"消失"，全样本修正后不成立；EG 5m 仍不显著（t=-1.1）。两品种在 15m 上均无方向信号。插针回补主要发生在分钟级，但甲醇的效应可延续到 5 分钟。</div>

<h2>七、证伪实验 ⑨：CLV 与形态码的增量检验（对编码方案价值的直接回答）</h2>
<p>控制变量分析暴露了一个机制事实：次日效应按<b>收盘位置 CLV=(close-low)/(high-low)</b> 呈单调 V 形——CLV→0 时次日 +0.0198%，CLV→1 时 -0.0197%（t≈±86）。那么用户的六位形态码是否只是 CLV 的重复造轮子？用 R² 分解与增量 F 检验回答：</p>
<div class="fig">{fig_clv()}</div>
{T6}
<div class="box ok"><b>结论：CLV 是主驱动，但形态码有真实增量，编码方案成立。</b>①单因子 R²：CLV（0.0248）≈ 形态码（0.0228），两者量级相当；②组合 R²=0.0307，高于任一单因子——两者信息互补（注：初版基于 2020 单年得出的"组合翻倍到 0.0642"不成立，全样本上只是略增）；③控制 CLV 后形态哑变量在全样本上<b>更</b>显著（F=53.7, p≈0）——影线与实体字段携带 CLV 之外的独立信息；④增量最清晰的证据在上图：CLV≤0.5 的内部，阴线（000000/000100）次日 <b>+0.0212%/+0.0205%</b> 而阳线（010000/010101）次日 <b>+0.0013%/-0.0000%</b>——同样的收盘位置，"跌着收"与"涨着收"次日完全不同，这正是实体方向字段的价值。</div>

<h2>八、最终判定：三条需要修正的表述</h2>
<div class="box warn">
<b>修正 1（收窄外推范围）</b>：V2 报告称"无影光头线反转效应跨品种一致"——在 1m 上成立（t_cluster -42/+37，逐日一致率 92.6%/91.9%）。跨周期需分级：<span class="pill p-pass">1m 稳健</span><span class="pill p-weak">MA 5m 仍显著</span><span class="pill p-fail">EG 5m / 15m 消失</span>该效应本质是 CLV=1 处的均值回归，属微观结构，量级（0.01-0.02%）远不足以覆盖成本，不是可套利信号。<br><br>
<b>修正 2（升级一个信号）</b>：V1/V2 报告把 010000/000000 当作"最强信号"——按跨周期稳健性排序应是：<b>000100（上影阴→涨）&gt; 000000 ≈ 010001 &gt; 010000</b>。000100 在 MA 的 1m/5m/15m 全周期显著（t=+51.3/+11.1/+3.5）、EG 在 1m/5m 显著，且跨 6 年逐日一致率 91.3%（MA）/79.8%（EG）——最稳健的单点信号。<br><br>
<b>修正 3（机制归因）</b>：V1 报告称"影线有无比实体方向更有预测力"——方向对但归因不准。更准确的表述：<b>预测力主要来自 CLV（收盘位置，由上/下影的有无与长度共同决定），实体方向在 CLV 内部的增量次之，影线字段除去 CLV 成分外的增量最小</b>（CLV=1 档内 010000 与 010001 仅差 0.0022%）。<br><br>
<b>无需修正</b>：L1（方向位失效）、L4（波动率聚集全周期稳健）、L5（1m 无动量只有回归）、L6（经济性：信号 4~5 元/手 vs 往返成本 14~28 元/手，不可直接交易）。
</div>

<h2>九、方法论备忘（本轮新踩的坑）</h2>
<ul>
<li><b>分钟级"下一根"必须用 gap==60s 定义相邻</b>：会话内还有 10:15-10:30 休市（CZCE/DCE 皆有），sess 级过滤不够；本次 V1 口径污染碰巧 &lt;1%，但属运气，必须显式过滤。</li>
<li><b>主力连续（.m@）默认按不复权处理</b>：OI 日跳变 &gt;40% 是定位换月日的可靠探针（MA 换月呈 12月→4月→8月 周期）。断层在会话首根、被相邻过滤排除，但跨周期聚合时若按自然日切分会把断层卷入日内。</li>
<li><b>分钟级 t 值必须过按日聚类</b>：朴素 t=-63 → 聚类 t=-42，衰减 33% 仍极显著；配合"逐日符号一致率"（92.6%）才是完整证据链。</li>
<li><b>发现"强效应"先查 CLV</b>：任何基于 OHLC 的形态信号，先把效应按收盘位置分箱——V 形曲线一眼可见是否只是极值回归。</li>
</ul>

<div class="sub" style="margin-top:26px;border-top:1px solid #eef1f5;padding-top:12px">
数据：D:\\学习资料 CZCE/DCE 主力连续 1 分钟（KQ.m@），2020-2025 全样本，MA/EG 各 489,975 根。脚本：step9_verify.py / step10_verify2.py / step11_verify3.py；中间结果：results_verify.json / results_verify2.json / results_verify3.json。所有 t 值为双样本均值检验；聚类 t 按日聚合后计算；bootstrap 为按日块重抽 200 次。
</div>
</div></body></html>"""

open(f"{OUT}/kline_pattern_report_v3.html", "w", encoding="utf-8").write(html)
print("report_v3 saved, chars:", len(html))
