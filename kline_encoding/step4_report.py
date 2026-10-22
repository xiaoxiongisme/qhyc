# -*- coding: utf-8 -*-
"""Step 4: 生成自包含 HTML 报告（内嵌SVG图表）"""
import json, html

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
R = json.load(open(f"{OUT}/analysis_results.json", encoding="utf-8"))
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
SHORT = {"000000": "跌·无·无", "000001": "跌·无·有", "000100": "跌·有·无", "000101": "跌·有·有",
         "010000": "涨·无·无", "010001": "涨·无·有", "010100": "涨·有·无", "010101": "涨·有·有"}
FULL = {"000000": "阴线·光头光脚", "000001": "阴线·光头锤形(带下影)", "000100": "阴线·光脚(带上影)", "000101": "阴线·上下影",
        "010000": "阳线·光头光脚", "010001": "阳线·光头锤形(带下影)", "010100": "阳线·光脚(带上影)", "010101": "阳线·上下影"}
C_MA, C_EG = "#2456a6", "#7c3aed"
RED, GREEN = "#c0392b", "#1e8449"   # 中式配色: 涨红 跌绿
INK, SUB, PANEL, BORDER = "#1f2329", "#646a73", "#f7f8fa", "#e3e6ea"

def esc(s): return html.escape(str(s))

def svg_grouped_bars(title_note=""):
    """形态分布: MA vs EG 分组柱状图"""
    W, H, pl, pr, pt, pb = 680, 300, 46, 12, 34, 46
    cw, ch = W - pl - pr, H - pt - pb
    vmax = max(max(R[s]["state_pct"].values()) for s in ("MA", "EG")) * 1.15
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif">']
    p.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>')
    for i in range(6):
        v = vmax * i / 5
        y = pt + ch - ch * v / vmax
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{W-pr}" y2="{y:.1f}" stroke="{BORDER}" stroke-width="1"/>')
        p.append(f'<text x="{pl-6}" y="{y+3:.1f}" font-size="10" fill="{SUB}" text-anchor="end">{v:.0f}%</text>')
    gw = cw / 8
    for i, s in enumerate(STATES):
        x0 = pl + i * gw
        for j, (sym, col) in enumerate((("MA", C_MA), ("EG", C_EG))):
            v = R[sym]["state_pct"][s]
            bh = ch * v / vmax
            bw = gw * 0.32
            x = x0 + gw * (0.18 + j * 0.36)
            p.append(f'<rect x="{x:.1f}" y="{pt+ch-bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}" rx="2"/>')
            p.append(f'<text x="{x+bw/2:.1f}" y="{pt+ch-bh-4:.1f}" font-size="9" fill="{col}" text-anchor="middle">{v:.1f}</text>')
        p.append(f'<text x="{x0+gw/2:.1f}" y="{pt+ch+14:.1f}" font-size="9.5" fill="{INK}" text-anchor="middle">{SHORT[s]}</text>')
        p.append(f'<text x="{x0+gw/2:.1f}" y="{pt+ch+26:.1f}" font-size="8" fill="{SUB}" text-anchor="middle">{s}</text>')
    p.append(f'<line x1="{pl}" y1="{pt+ch}" x2="{W-pr}" y2="{pt+ch}" stroke="{INK}" stroke-width="1"/>')
    lx = pl + 8
    for sym, col in (("MA", C_MA), ("EG", C_EG)):
        name = "甲醇 MA" if sym == "MA" else "乙二醇 EG"
        p.append(f'<rect x="{lx}" y="10" width="10" height="10" fill="{col}" rx="2"/><text x="{lx+14}" y="19" font-size="11" fill="{INK}">{name}</text>')
        lx += 110
    p.append('</svg>')
    return "".join(p)

def svg_diverging():
    """各形态 -> 下一根K线涨跌幅条件均值 ( diverging bars )"""
    W, H, pl, pr, pt, pb = 680, 300, 46, 12, 30, 46
    cw, ch = W - pl - pr, H - pt - pb
    ma = {x["state"]: x for x in R["extra"]["MA"]["state_strict"]}
    eg = {x["state"]: x for x in R["extra"]["EG"]["state_strict"]}
    vmax = max(abs(x["mean"]) for x in list(ma.values()) + list(eg.values())) * 1.2
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    y0 = pt + ch / 2
    for k in range(-4, 5):
        v = vmax * k / 4
        y = y0 - ch / 2 * v / vmax
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{W-pr}" y2="{y:.1f}" stroke="{BORDER}" stroke-width="1"/>')
        p.append(f'<text x="{pl-6}" y="{y+3:.1f}" font-size="9" fill="{SUB}" text-anchor="end">{v:+.2f}%</text>')
    gw = cw / 8
    for i, s in enumerate(STATES):
        x0 = pl + i * gw
        for j, (src, hatch) in enumerate((("MA", None), ("EG", C_EG))):
            v = (ma if src == "MA" else eg)[s]["mean"]
            bh = ch / 2 * abs(v) / vmax
            bw = gw * 0.3
            x = x0 + gw * (0.2 + j * 0.34)
            col = RED if v > 0 else GREEN
            if src == "EG":
                p.append(f'<rect x="{x:.1f}" y="{y0 if v<0 else y0-bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}" fill-opacity="0.45" stroke="{col}" stroke-width="1" rx="2"/>')
            else:
                p.append(f'<rect x="{x:.1f}" y="{y0 if v<0 else y0-bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}" rx="2"/>')
        p.append(f'<text x="{x0+gw/2:.1f}" y="{pt+ch+14:.1f}" font-size="9.5" fill="{INK}" text-anchor="middle">{SHORT[s]}</text>')
    p.append(f'<text x="{pl}" y="{y0-6:.1f}" font-size="10" fill="{RED}">下一根平均上涨 →</text>')
    p.append(f'<text x="{pl}" y="{y0+14:.1f}" font-size="10" fill="{GREEN}">下一根平均下跌 →</text>')
    p.append(f'<line x1="{pl}" y1="{y0}" x2="{W-pr}" y2="{y0}" stroke="{INK}" stroke-width="1.2"/>')
    p.append(f'<text x="{pl+330}" y="16" font-size="10" fill="{INK}">■ 甲醇(实心)　▨ 乙二醇(半透明)</text>')
    p.append('</svg>')
    return "".join(p)

def svg_amp_acf():
    """左: 各形态平均振幅; 右: 自相关"""
    W, H = 680, 280
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    # 左图: amp by state
    pl, pt, ch, cw = 40, 34, 190, 300
    geo_ma = {g["state"]: g for g in R["extra"]["MA"]["geometry"]}
    geo_eg = {g["state"]: g for g in R["extra"]["EG"]["geometry"]}
    vmax = max(g["amp_mean"] for g in list(geo_ma.values()) + list(geo_eg.values())) * 1.15
    p.append(f'<text x="{pl}" y="18" font-size="11.5" font-weight="600" fill="{INK}">各形态平均振幅（%，越高=越波动）</text>')
    for i in range(5):
        v = vmax * i / 4
        y = pt + ch - ch * v / vmax
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+cw}" y2="{y:.1f}" stroke="{BORDER}"/>')
        p.append(f'<text x="{pl-5}" y="{y+3:.1f}" font-size="9" fill="{SUB}" text-anchor="end">{v:.2f}</text>')
    gw = cw / 8
    for i, s in enumerate(STATES):
        x0 = pl + i * gw
        for j, (g, col) in enumerate(((geo_ma[s], C_MA), (geo_eg[s], C_EG))):
            v = g["amp_mean"]; bh = ch * v / vmax; bw = gw * 0.3
            x = x0 + gw * (0.2 + j * 0.34)
            p.append(f'<rect x="{x:.1f}" y="{pt+ch-bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}" rx="2"/>')
        p.append(f'<text x="{x0+gw/2:.1f}" y="{pt+ch+13:.1f}" font-size="8.5" fill="{INK}" text-anchor="middle">{SHORT[s]}</text>')
    p.append(f'<line x1="{pl}" y1="{pt+ch}" x2="{pl+cw}" y2="{pt+ch}" stroke="{INK}"/>')
    # 右图: acf
    pl2 = pl + cw + 60
    cw2 = W - pl2 - 30
    p.append(f'<text x="{pl2}" y="18" font-size="11.5" font-weight="600" fill="{INK}">自相关（lag=分钟数）</text>')
    y0 = pt + ch / 2
    for v in (-0.3, -0.15, 0, 0.15, 0.3):
        y = y0 - ch / 2 * v / 0.3
        p.append(f'<line x1="{pl2}" y1="{y:.1f}" x2="{W-30}" y2="{y:.1f}" stroke="{BORDER}"/>')
        p.append(f'<text x="{pl2-5}" y="{y+3:.1f}" font-size="9" fill="{SUB}" text-anchor="end">{v:+.2f}</text>')
    series = [("MA", R["MA"]["acf_chg"][:10], R["MA"]["acf_abschg"][:10], C_MA, "MA 涨跌幅"),
              ("EG", R["EG"]["acf_chg"][:10], R["EG"]["acf_abschg"][:10], C_EG, "EG 涨跌幅")]
    for sym, acf, acfa, col, _ in series:
        pts = " ".join(f"{pl2 + cw2*(k+1)/11:.1f},{y0 - ch/2*acf[k]/0.3:.1f}" for k in range(10))
        p.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.8"/>')
        pts2 = " ".join(f"{pl2 + cw2*(k+1)/11:.1f},{y0 - ch/2*acfa[k]/0.3:.1f}" for k in range(min(len(acfa), 10)))
        p.append(f'<polyline points="{pts2}" fill="none" stroke="{col}" stroke-width="1.4" stroke-dasharray="4 3"/>')
    for k in range(1, 11):
        p.append(f'<text x="{pl2+cw2*k/11:.1f}" y="{pt+ch+13:.1f}" font-size="8.5" fill="{INK}" text-anchor="middle">{k}</text>')
    p.append(f'<line x1="{pl2}" y1="{y0}" x2="{W-30}" y2="{y0}" stroke="{INK}" stroke-width="1.2"/>')
    p.append(f'<line x1="{pl2}" y1="{pt+ch}" x2="{W-30}" y2="{pt+ch}" stroke="{INK}"/>')
    p.append(f'<text x="{pl2+8}" y="{pt+8}" font-size="9.5" fill="{C_MA}">— 甲醇 涨跌幅acf</text>')
    p.append(f'<text x="{pl2+8}" y="{pt+20}" font-size="9.5" fill="{C_MA}">- - 甲醇 |涨跌幅|acf</text>')
    p.append(f'<text x="{pl2+140}" y="{pt+8}" font-size="9.5" fill="{C_EG}">— 乙二醇 涨跌幅acf</text>')
    p.append(f'<text x="{pl2+140}" y="{pt+20}" font-size="9.5" fill="{C_EG}">- - 乙二醇 |涨跌幅|acf</text>')
    p.append('</svg>')
    return "".join(p)

def svg_streak():
    """连续同向K线 -> 下一根严格上涨概率"""
    W, H, pl, pt, ch, cw = 680, 260, 46, 30, 170, 600
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    lo, hi = 0.30, 0.50
    for v in (0.30, 0.35, 0.40, 0.45, 0.50):
        y = pt + ch - ch * (v - lo) / (hi - lo)
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+cw}" y2="{y:.1f}" stroke="{BORDER}"/>')
        p.append(f'<text x="{pl-6}" y="{y+3:.1f}" font-size="9" fill="{SUB}" text-anchor="end">{v:.0%}</text>')
    for sym, col, name in (("MA", C_MA, "甲醇"), ("EG", C_EG, "乙二醇")):
        rows = [x for x in R["extra"][sym]["streak"]]
        pts = " ".join(f"{pl + cw*x['k']/5:.1f},{pt + ch - ch*(x['p_up_strict']-lo)/(hi-lo):.1f}" for x in rows)
        p.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2"/>')
        for x in rows:
            cx = pl + cw * x["k"] / 5
            cy = pt + ch - ch * (x["p_up_strict"] - lo) / (hi - lo)
            p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{col}"/>')
            p.append(f'<text x="{cx:.1f}" y="{cy-8:.1f}" font-size="9" fill="{col}" text-anchor="middle">{x["p_up_strict"]:.1%}</text>')
            p.append(f'<text x="{cx:.1f}" y="{pt+ch+14:.1f}" font-size="9" fill="{SUB}" text-anchor="middle">k={x["k"]} (n={x["n"]:,})</text>')
    yb = pt + ch - ch * (0.5 - lo) / (hi - lo)
    p.append(f'<line x1="{pl}" y1="{yb:.1f}" x2="{pl+cw}" y2="{yb:.1f}" stroke="{SUB}" stroke-dasharray="5 4"/>')
    p.append(f'<text x="{pl+cw-4}" y="{yb-5:.1f}" font-size="9.5" fill="{SUB}" text-anchor="end">基准≈50%（剔除平盘后公平硬币）</text>')
    p.append('</svg>')
    return "".join(p)

def svg_heatmap(sym):
    """8x8 状态转移 lift 热图"""
    P = R[sym]["trans_prob"]; L = R[sym]["trans_lift"]; C = R[sym]["trans_counts"]
    marg = [R[sym]["state_pct"][s] / 100 for s in STATES]
    cell, pl, pt = 46, 58, 44
    W = pl + 8 * cell + 130; H = pt + 8 * cell + 40
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    name = "甲醇 MA" if sym == "MA" else "乙二醇 EG"
    p.append(f'<text x="8" y="18" font-size="12" font-weight="600" fill="{INK}">{name}：状态转移提升度 lift = P(下一形态|当前形态) ÷ P(下一形态)</text>')
    p.append(f'<text x="8" y="32" font-size="9.5" fill="{SUB}">红色=高于基准（该转移更易发生），绿色=低于基准；加粗= |z|>3 显著；格内数字=lift</text>')
    for i in range(8):
        y = pt + i * cell
        p.append(f'<text x="{pl-6}" y="{y+cell/2+3:.1f}" font-size="9" fill="{INK}" text-anchor="end">{SHORT[STATES[i]]}</text>')
        for j in range(8):
            x = pl + j * cell
            lift = L[i][j]
            a = min(abs(lift - 1) * 2.2, 0.85)
            col = RED if lift > 1 else GREEN
            p.append(f'<rect x="{x}" y="{y}" width="{cell-1}" height="{cell-1}" fill="{col}" fill-opacity="{a:.2f}" stroke="#ffffff"/>')
            z = (P[i][j] - marg[j]) / max((marg[j] * (1 - marg[j]) * (1 / sum(C[i]) + 1 / R[sym]["n"])) ** 0.5, 1e-9)
            fw = "700" if abs(z) > 3 else "400"
            tc = INK if abs(z) > 3 else SUB
            p.append(f'<text x="{x+cell/2:.1f}" y="{y+cell/2+3:.1f}" font-size="9" font-weight="{fw}" fill="{tc}" text-anchor="middle">{lift:.2f}</text>')
    for j in range(8):
        p.append(f'<text x="{pl+j*cell+cell/2:.1f}" y="{pt+8*cell+12:.1f}" font-size="8.5" fill="{INK}" text-anchor="middle" transform="rotate(0)">{SHORT[STATES[j]]}</text>')
    p.append(f'<text x="{pl}" y="{pt-6}" font-size="9" fill="{SUB}">当前形态 ↓ ／ 下一根形态 →</text>')
    p.append('</svg>')
    return "".join(p)

def fmt_tbl(headers, rows, widths=None):
    th = "".join(f'<th style="{"width:"+str(w)+"px" if w else ""}">{h}</th>' for h, w in zip(headers, widths or [None] * len(headers)))
    trs = ""
    for r in rows:
        tds = "".join(f"<td>{c}</td>" for c in r)
        trs += f"<tr>{tds}</tr>"
    return f'<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'

# ---------------- 组装 HTML ----------------
MA, EG = R["MA"], R["EG"]
ma_st = {x["state"]: x for x in R["extra"]["MA"]["state_strict"]}
eg_st = {x["state"]: x for x in R["extra"]["EG"]["state_strict"]}
ma_geo = {g["state"]: g for g in R["extra"]["MA"]["geometry"]}
eg_geo = {g["state"]: g for g in R["extra"]["EG"]["geometry"]}

# T1 编码方案表
t1_rows = []
for s in STATES:
    t1_rows.append([f"<code>{s}</code>", FULL[s], f"{MA['state_counts'][s]:,}", f"{MA['state_pct'][s]:.1f}%",
                    f"{EG['state_counts'][s]:,}", f"{EG['state_pct'][s]:.1f}%"])
T1 = fmt_tbl(["六位编码", "形态含义", "甲醇计数", "甲醇占比", "乙二醇计数", "乙二醇占比"], t1_rows)

# T3 每形态->下一根
t3_rows = []
for s in STATES:
    a, b = ma_st[s], eg_st[s]
    t3_rows.append([f"<code>{s}</code> {SHORT[s]}",
                    f"{a['p_up_strict']:.1%}", f"{a['p_dn_strict']:.1%}", f"{a['p_doji']:.1%}",
                    f"{a['mean']:+.4f}%", f"t={a['t']:+.1f}",
                    f"{b['p_up_strict']:.1%}", f"{b['p_dn_strict']:.1%}", f"{b['p_doji']:.1%}",
                    f"{b['mean']:+.4f}%", f"t={b['t']:+.1f}"])
T3 = fmt_tbl(["形态", "涨", "跌", "平", "均值", "t值", "涨", "跌", "平", "均值", "t值"], t3_rows)

# T4 几何
t4_rows = []
for s in STATES:
    a, b = ma_geo[s], eg_geo[s]
    t4_rows.append([f"<code>{s}</code> {SHORT[s]}", f"{a['amp_mean']:.3f}%", f"{a['body_ratio']:.0%}", f"{a['up_ratio']:.0%}", f"{a['lo_ratio']:.0%}",
                    f"{b['amp_mean']:.3f}%", f"{b['body_ratio']:.0%}", f"{b['up_ratio']:.0%}", f"{b['lo_ratio']:.0%}"])
T4 = fmt_tbl(["形态", "振幅", "实体占比", "上影占比", "下影占比", "振幅", "实体占比", "上影占比", "下影占比"], t4_rows)

# T5 显著转移 (MA top10 + EG top10 合并去重)
sig_rows = []
seen = set()
for x in MA["sig_trans"][:10] + EG["sig_trans"][:10]:
    k = (x["from"], x["to"])
    if k in seen: continue
    seen.add(k)
    sig_rows.append([f"<code>{x['from']}</code> → <code>{x['to']}</code>",
                     f"{SHORT[x['from']]} → {SHORT[x['to']]}",
                     f"{x['p']:.1%}", f"{x['lift']:.2f}", f"z={x['z']:+.1f}"])
T5 = fmt_tbl(["转移", "含义", "条件概率", "提升度", "显著性"], sig_rows[:14])

# T6 时段
t6_rows = []
for a, b in zip(MA["segments"], EG["segments"]):
    t6_rows.append([a["seg"], f"{a['n']:,}", f"{a['up_rate']:.1%}", f"{a['amp_med']:.3f}%", f"{a['p_upper']:.1%}", f"{a['p_lower']:.1%}",
                    f"{b['up_rate']:.1%}", f"{b['amp_med']:.3f}%", f"{b['p_upper']:.1%}", f"{b['p_lower']:.1%}"])
T6 = fmt_tbl(["时段", "甲醇K线数", "上涨率*", "振幅中位", "上影率", "下影率", "上涨率*", "振幅中位", "上影率", "下影率"], t6_rows)

# T7 两形态序列
t7_rows = []
for a, b in zip(R["extra"]["MA"]["pattern_best"][:6], R["extra"]["EG"]["pattern_best"][:6]):
    t7_rows.append([a["pair"].replace("|", " → "), f"{a['n']:,}", f"{a['mean_next']:+.4f}%", f"{a['p_up_strict']:.1%}",
                    b["pair"].replace("|", " → "), f"{b['n']:,}", f"{b['mean_next']:+.4f}%", f"{b['p_up_strict']:.1%}"])
T7 = fmt_tbl(["甲醇两连形态", "n", "下一根均值", "下一根涨率", "乙二醇两连形态", "n", "下一根均值", "下一根涨率"], t7_rows)

# T8 经济性
price_ma, price_eg = 1977, 3898
edge_ma = ma_st["010000"]["mean"] / 100 * price_ma * 10
edge_eg = eg_st["010000"]["mean"] / 100 * price_eg * 10
edge_ma2 = ma_st["000100"]["mean"] / 100 * price_ma * 10
edge_eg2 = eg_st["000100"]["mean"] / 100 * price_eg * 10
amp_ma_y = MA["amp_med"] / 100 * price_ma * 10
T8 = fmt_tbl(["信号（出现后做空/做多下一根）", "甲醇信号幅度", "折合每手", "乙二醇信号幅度", "折合每手"],
             [["光头光脚阳线 010000 后做空", f"{ma_st['010000']['mean']:+.4f}%", f"{edge_ma:+.1f} 元/手", f"{eg_st['010000']['mean']:+.4f}%", f"{edge_eg:+.1f} 元/手"],
              ["带上影阴线 000100 后做多", f"{ma_st['000100']['mean']:+.4f}%", f"{edge_ma2:+.1f} 元/手", f"{eg_st['000100']['mean']:+.4f}%", f"{edge_eg2:+.1f} 元/手"],
              ["参考：1分钟典型振幅(中位)", f"{MA['amp_med']:.3f}%", f"≈{amp_ma_y:.0f} 元/手", f"{EG['amp_med']:.3f}%", f"≈{EG['amp_med']/100*price_eg*10:.0f} 元/手"],
              ["参考：单次往返成本(手续费+1跳滑点)", "约 14~18 元/手", "", "约 18~28 元/手", ""]])

shared_rows = []
for x in R["shared_trans"][:10]:
    shared_rows.append([f"<code>{x['from']}</code> → <code>{x['to']}</code>", SHORT[x["from"]] + " → " + SHORT[x["to"]],
                        f"{x['lift_MA']:.2f}", f"{x['lift_EG']:.2f}"])
T9 = fmt_tdb = fmt_tbl(["转移", "含义", "甲醇lift", "乙二醇lift"], shared_rows)

html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>一分钟K线六位二进制编码：甲醇/乙二醇2020年规律分析</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#fff;color:{INK};max-width:1080px;margin:0 auto;padding:28px 20px 60px;line-height:1.75;font-size:14.5px}}
h1{{font-size:24px;border-bottom:3px solid {INK};padding-bottom:12px;margin-bottom:6px}}
h2{{font-size:19px;margin-top:44px;border-left:5px solid {C_MA};padding-left:10px}}
h3{{font-size:16px;margin-top:26px}}
.meta{{color:{SUB};font-size:13px;margin-bottom:24px}}
table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px}}
th{{background:{PANEL};border:1px solid {BORDER};padding:6px 8px;text-align:left;white-space:nowrap}}
td{{border:1px solid {BORDER};padding:6px 8px}}
tr:nth-child(even) td{{background:#fafbfc}}
code{{background:{PANEL};padding:1px 5px;border-radius:3px;font-size:12.5px;font-family:Consolas,monospace}}
.panel{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;padding:14px 18px;margin:16px 0}}
.finding{{border:1px solid {BORDER};border-left:5px solid {C_MA};border-radius:6px;padding:12px 18px;margin:18px 0;background:#fff}}
.finding h3{{margin-top:0;color:{C_MA}}}
.tag{{display:inline-block;background:{C_MA};color:#fff;font-size:11px;border-radius:3px;padding:1px 7px;margin-right:8px;vertical-align:middle}}
.tag.eg{{background:{C_EG}}}
.warn{{border-left-color:#d4380d;background:#fff7f0}}
.warn h3{{color:#d4380d}}
.small{{font-size:12.5px;color:{SUB}}}
.svgbox{{border:1px solid {BORDER};border-radius:8px;padding:10px;margin:14px 0;background:#fff}}
ul{{margin:8px 0 8px 4px}} li{{margin:4px 0}}
.footer{{margin-top:50px;border-top:2px solid {BORDER};padding-top:14px;font-size:12.5px;color:{SUB}}}
</style></head><body>

<h1>一分钟K线「六位二进制编码」转换与规律分析</h1>
<div class="meta">数据：甲醇 MA（CZCE）、乙二醇 EG（DCE）2020年主力连续1分钟K线 · 各 75,795 根 · 243 个交易日 · 分析日期 2026-10-16</div>

<div class="panel">
<b>你的编码方案（本报告严格按此实现）</b><br>
一根1分钟K线 → <b>四个数</b>：<code>(六位形态码, 起始位, 涨跌幅%, 振幅%)</code>
<ul>
<li><b>六位形态码</b> = 实体方向(2位) + 上影线(2位) + 下影线(2位)，每位段 <code>00</code>=否、<code>01</code>=是</li>
<li>实体方向：<code>00</code>=跌(close&lt;open)，<code>01</code>=涨(close≥open，<b>含平盘</b>)</li>
<li>上影线：<code>01</code>=有 (high &gt; max(open,close))；下影线：<code>01</code>=有 (low &lt; min(open,close))</li>
<li>涨跌幅 = (close−open)/open×100%；振幅 = (high−low)/open×100%</li>
</ul>
<span class="small">注：该方案中每位段虽用2位表示，实际只承载1比特信息（8种形态=3比特）；影线只记「有无」不记长度，故相对原始OHLC是<b>有损压缩</b>（若需无损还原high/low，需补记上、下影线长度两个数）。已通过随机3000行×2品种逐行复核，转换与原始OHLC完全一致。</span>
</div>

<h2>一、数据概览</h2>
<table>
<tr><th>项目</th><th>甲醇 MA</th><th>乙二醇 EG</th></tr>
<tr><td>K线数（2020全年）</td><td>{MA['n']:,}</td><td>{EG['n']:,}</td></tr>
<tr><td>交易日 / 有夜盘交易日</td><td>243 / 176（67天无夜盘：春节及疫情期间、年末）</td><td>243 / 176</td></tr>
<tr><td>平盘K线（开=收）占比</td><td><b>{MA['doji_rate']:.1%}</b></td><td><b>{EG['doji_rate']:.1%}</b></td></tr>
<tr><td>「上涨」占比（含平盘）/ 剔除平盘后严格上涨</td><td>{MA['up_rate']:.1%} / <b>{MA['up_rate_exdoji']:.1%}</b></td><td>{EG['up_rate']:.1%} / <b>{EG['up_rate_exdoji']:.1%}</b></td></tr>
<tr><td>涨跌幅绝对值中位 / 振幅中位</td><td>{MA['chg_abs_med']:.3f}% / {MA['amp_med']:.3f}%</td><td>{EG['chg_abs_med']:.3f}% / {EG['amp_med']:.3f}%</td></tr>
<tr><td>相邻K线价格连续率（openₜ=closeₜ₋₁）</td><td>{MA['cont_rate']:.2%}</td><td>{EG['cont_rate']:.2%}</td></tr>
<tr><td>形态分布熵 H(S)</td><td>{R['extra']['MA']['entropy_bits']:.2f} 比特（上限3）</td><td>{R['extra']['EG']['entropy_bits']:.2f} 比特</td></tr>
</table>

<h2>二、八种形态的分布</h2>
<div class="svgbox">{svg_grouped_bars()}</div>
{T1}
<p>两个品种的形态分布高度一致：<b>「涨·有上影·有下影」(010101) 都是第一大形态</b>（甲醇19.2%、乙二醇23.9%），「跌·有上影·有下影」(000101) 都是最小众形态。熵接近3比特，说明8种形态都被充分使用——这套编码对1分钟数据是「饱满」的，没有空转的状态。</p>

<h2>三、发现的规律</h2>

<div class="finding">
<h3><span class="tag">规律1</span>平盘K线占四分之一，「方向位」在1分钟级别基本失效</h3>
<p>甲醇 <b>{MA['doji_rate']:.1%}</b>、乙二醇 {EG['doji_rate']:.1%} 的1分钟K线开盘=收盘。若按 close≥open 记为「涨」，甲醇全年「上涨」K线高达 {MA['up_rate']:.1%}；但<b>剔除平盘后，严格上涨仅 {MA['up_rate_exdoji']:.1%}（乙二醇 {EG['up_rate_exdoji']:.1%}）——几乎是精确的公平硬币</b>。方向上几乎无漂移可挖，真正的信息在影线字段与连续结构里。</p>
</div>

<div class="finding">
<h3><span class="tag">规律2</span><span class="tag eg">跨品种一致</span>「光头光脚」线之后必然回调——无影线=单边透支</h3>
<p><b>涨·无上影·无下影（010000）</b>意味着这一分钟价格从开盘径直收至最高（low=open, high=close，零回踩）。这种「零回踩单边线」出现后：</p>
<ul>
<li>甲醇：下一分钟<b>严格下跌 {ma_st['010000']['p_dn_strict']:.1%}</b> vs 上涨 {ma_st['010000']['p_up_strict']:.1%}，条件均值 <b>{ma_st['010000']['mean']:+.4f}%</b>（t={ma_st['010000']['t']:+.1f}，n={ma_st['010000']['n']:,}）</li>
<li>乙二醇：下一分钟严格下跌 {eg_st['010000']['p_dn_strict']:.1%} vs 上涨 {eg_st['010000']['p_up_strict']:.1%}，条件均值 <b>{eg_st['010000']['mean']:+.4f}%</b>（t={eg_st['010000']['t']:+.1f}）</li>
</ul>
<p>镜像规律：<b>跌·无上影·无下影（000000，零反弹单边下跌）</b>之后，甲醇下一分钟严格上涨 {ma_st['000000']['p_up_strict']:.1%}、均值 <b>{ma_st['000000']['mean']:+.4f}%</b>（t={ma_st['000000']['t']:+.1f}）；乙二醇 {eg_st['000000']['p_up_strict']:.1%}、{eg_st['000000']['mean']:+.4f}%（t={eg_st['000000']['t']:+.1f}）。<b>影线越少，下一根反转越强</b>——「没有影线」本身就是「这一分钟被单向打爆、下一分钟回补」的信号。</p>
</div>

<div class="finding">
<h3><span class="tag">规律3</span><span class="tag eg">跨品种一致</span>长下影阳线（010001）之后偏跌，长上影阴线（000100）之后偏涨</h3>
<p>传统K线语言里「长下影阳线」是强势信号，但在1分钟数据里它预示的是<b>下一根下跌</b>：甲醇 010001 之后严格下跌 {ma_st['010001']['p_dn_strict']:.1%} vs 上涨 {ma_st['010001']['p_up_strict']:.1%}，均值 {ma_st['010001']['mean']:+.4f}%（t={ma_st['010001']['t']:+.1f}）；乙二醇同向（{eg_st['010001']['mean']:+.4f}%，t={eg_st['010001']['t']:+.1f}）。反过来，<b>带上影的阴线（000100）之后偏涨</b>：甲醇 {ma_st['000100']['mean']:+.4f}%（t={ma_st['000100']['t']:+.1f}），乙二醇 {eg_st['000100']['mean']:+.4f}%（t={eg_st['000100']['t']:+.1f}）。</p>
<p class="small">解读：1分钟层面的「长下影」多是流动性冲击/止损扫单造成的瞬时插针，插完之后趋势延续原方向（跌），而非企稳。这是典型的 microstructure（微观结构）效应，与日线上「长下影=支撑」的经验相反——<b>同一形态在不同时间尺度含义相反</b>。</p>
</div>

<div class="finding">
<h3><span class="tag">规律4</span>最强的规律其实是波动率聚集：形态码同时是「波动率状态码」</h3>
<p>|涨跌幅| 的一阶自相关：甲醇 <b>{MA['acf_abschg'][0]:.3f}</b>、乙二醇 <b>{EG['acf_abschg'][0]:.3f}</b>，且到第5分钟仍有 {MA['acf_abschg'][4]:.2f}/{EG['acf_abschg'][4]:.2f}——大分钟之后是大分钟。与此同时，各形态的平均振幅相差2倍以上：</p>
<ul>
<li>双影线形态 000101 平均振幅 {ma_geo['000101']['amp_mean']:.3f}%（甲醇），是无影阳线 010000（{ma_geo['010000']['amp_mean']:.3f}%）的 <b>{ma_geo['000101']['amp_mean']/ma_geo['010000']['amp_mean']:.1f} 倍</b></li>
<li>也就是说：<b>读到六位码，就知道下一段大致多大的波动</b>——形态码天然带波动率状态信息，可用于仓位/过滤条件</li>
</ul>
<p>而涨跌幅（符号）自相关为负：甲醇 lag1 = {MA['acf_chg'][0]:.3f}，乙二醇 {EG['acf_chg'][0]:.3f}——1分钟层面是<b>均值回归</b>而非趋势。连胜不加速：连续1~4根严格阳线后，下一根严格上涨概率仅 {R['extra']['MA']['streak'][1]['p_up_strict']:.1%}→{R['extra']['MA']['streak'][4]['p_up_strict']:.1%}（甲醇），反而<b>低于</b>50%基准。</p>
</div>

<div class="finding">
<h3><span class="tag">规律5</span>状态转移有真实但很小的结构（互信息可证）</h3>
<p>八状态马尔可夫链：同形态延续率 {MA['same_state_rate']:.1%}/{EG['same_state_rate']:.1%}（随机基准12.5%，z={MA['same_state_z']:.1f}）；互信息 <b>MI(当前形态; 下一形态) = {MA['mi_state_next']:.3f} / {EG['mi_state_next']:.3f} 比特</b>（置换检验300次，null≈0.0005，p≈0）。方向预测力则微乎其微：MI(形态; 下一根涨跌) 仅 {MA['mi_state_nextsign']:.4f}/{EG['mi_state_nextsign']:.4f} 比特。<b>形态码预测的是「下一根长什么样、多大」，基本预测不了「涨还是跌」。</b></p>
</div>

<div class="svgbox">{svg_diverging()}</div>
{T3}
<p class="small">t值为对「下一根涨跌幅均值=0」的单样本t检验；|t|&gt;3 即统计显著（n均在4800以上）。甲醇效应量系统性大于乙二醇（甲醇1分钟更「糙」、插针更多）。</p>

<div class="svgbox">{svg_amp_acf()}</div>
{T4}

<div class="svgbox">{svg_streak()}</div>
<p class="small">横轴k=之前连续k根严格阳线（k=0为无条件基准）。两个品种完全同向：连涨之后不涨反跌——1分钟级别无动量，只有回归。</p>

<h3>状态转移矩阵（提升度热图）</h3>
<div class="svgbox">{svg_heatmap('MA')}</div>
<div class="svgbox">{svg_heatmap('EG')}</div>
{T5}
<h3>两个品种共同显著的转移（跨样本稳健）</h3>
{T9}
<p>甲醇的信息结构（0.198比特）明显强于乙二醇（0.049比特），但<b>上面规律2/3/4在两品种上完全同向</b>——这是「规律」而非单样本巧合的最直接证据。</p>

<h2>四、时段与开盘结构</h2>
{T6}
<p class="small">*上涨率含平盘。早盘9-10点振幅最大（甲醇 {MA['segments'][1]['amp_med']:.3f}% vs 夜盘 {MA['segments'][0]['amp_med']:.3f}%），符合开盘信息释放；各时段上涨率高度均匀，无显著时段方向性。会话首根（含午休后首根）平均涨跌幅 +{MA['first_bar_chg_mean']:.4f}%（t={MA['first_bar_t']:.1f}，不显著），平均跳空 +0.004%（标准差0.76%）——跳空对称、无系统性隔夜漂移。</p>

<h2>五、两连形态序列（马尔可夫之外的二阶模式）</h2>
{T7}
<p>最优/最差序列的共有特征仍然是<b>最后一根无上影阳线→跌、无下影阴线→涨</b>（即规律2/3），加入倒数第二根形态几乎没有增量——与「MI(形态;下一形态)很小」互为印证：<b>一阶信息已接近全部</b>，不必建高阶模型。</p>

<h2>六、经济性现实检验（重要）</h2>
{T8}
<div class="finding warn">
<h3>⚠ 结论：这些规律真实存在，但1分钟级别<b>不足以覆盖交易成本</b></h3>
<ul>
<li>规律2的信号幅度折算约 <b>4~5 元/手</b>（甲醇）、约 <b>4 元/手</b>（乙二醇）；而一次往返成本（2020年大致标准：甲醇开+平手续费约4元/手、乙二醇约8元/手，再加至少1跳滑点=10元/手）合计约 <b>14~28 元/手</b></li>
<li>信号幅度只有1分钟典型振幅的约 1/5（甲醇 23 元/手）至 1/9（乙二醇 36 元/手），信噪比太低，单独使用必亏</li>
<li><b>更有价值用法</b>：① 把六位码当作<b>特征/过滤器</b>喂给更大周期（5/15/60分钟）或组合模型——成本不变而信号空间更大；② 用形态码做<b>波动率状态识别</b>（规律4，效应最强最稳）；③ 用它做<b>仓位管理</b>（光头光脚线后降低加仓意愿）；④ 在1分钟级别做<b>执行算法</b>（判断插针/追单风险），而非方向预测</li>
</ul>
</div>

<h2>七、稳健性检查</h2>
<table>
<tr><th>检查项</th><th>结果</th></tr>
<tr><td>剔除极端K线（|涨跌幅|&gt;1%，甲醇15根/乙二醇7根，均为2020年真实事件日：1/8美伊冲突、3/10油价暴跌等会话开盘跳空）</td><td>核心结论逐位不变（P(涨|涨)等复算值完全一致）</td></tr>
<tr><td>上下半年拆分</td><td>动量/回归方向一致（甲醇 P(涨|涨)：{MA['h1_mom']:.3f} vs {MA['h2_mom']:.3f}；乙二醇 {EG['h1_mom']:.3f} vs {EG['h2_mom']:.3f}）</td></tr>
<tr><td>互信息置换检验（300次）</td><td>甲醇 p≈0.000，乙二醇 p≈0.000，非随机</td></tr>
<tr><td>跨品种一致性</td><td>规律1/2/3/4 两品种同向，非单样本现象</td></tr>
<tr><td>数据连续性</td><td>99.2% 相邻K线 openₜ=closeₜ₋₁，无缺失、无全NaN段；无夜盘日为2020-01-23至12-31间的67天（春节+疫情+元旦假期）</td></tr>
</table>

<h2>八、结论</h2>
<ol>
<li><b>编码本身成立且好用</b>：8种形态对1分钟数据饱满（熵2.9比特），四元组 (码,起始位,涨跌幅,振幅) 是有损但紧凑的描述符；若追求无损需补两个影线长度。</li>
<li><b>找到的核心规律</b>：① 1分钟方向≈公平硬币，平盘占1/4，方向位信息量低；② <b>无影线（光头光脚）单边线之后强反转</b>，长下影阳线之后偏跌、长上影阴线之后偏涨——影线有无比实体方向更有预测力；③ 波动率聚集是最强规律，形态码即波动率状态码；④ 1分钟无动量、只有回归；⑤ 一阶（单形态）信息已接近全部，二阶序列无增量。</li>
<li><b>诚实的边界</b>：以上是单年、两品种、1分钟粒度、主力连续（含换月）样本上的统计描述；规律2/3的信号量级小于交易成本，<b>不构成直接可交易的策略</b>，但作为特征工程、波动率状态识别与执行风控的输入是合格的。建议下一步：同一编码在5/15分钟级别、以及2021-2025年样本上复验（换月跳点需先处理）。</li>
</ol>

<div class="footer">
<p><b>交付文件</b>（E:/抖音分析/2026-10-16-19-50-44/kline_encoding/）：</p>
<ul class="small">
<li><code>MA_2020_1m_encoded.csv</code> / <code>EG_2020_1m_encoded.csv</code> — 转换后数据：datetime, code(六位字符串), code_dec(十进制定律0-63), open(起始位), chg_pct(涨跌幅%), amp_pct(振幅%)，各75,795行。<b>注意：用 pandas 读取时需指定 code 列为字符串（dtype=&#123;'code': str&#125;），否则前导零会被当成数字吃掉（Excel 同理，此时可用 code_dec 列）</b></li>
<li><code>MA_2020_1m_raw.csv</code> / <code>EG_2020_1m_raw.csv</code> — 2020年原始1分钟OHLCV（含持仓）</li>
<li><code>step1_extract_2020.py</code> / <code>step2_encode.py</code> / <code>step3_analyze.py</code> / <code>step3b_analyze_extra.py</code> / <code>step4_report.py</code> — 全流程可复现脚本</li>
<li><code>analysis_results.json</code> — 全部统计量</li>
</ul>
<p class="small">数据来源：本地期货1分钟行情（主力连续，KQ数据），2020-01-02 至 2020-12-31。主力连续含换月拼接，极端K线已核查均为真实事件。本报告为数据统计研究，不构成任何投资建议；市场有风险，决策需独立。</p>
</div>
</body></html>"""

with open(f"{OUT}/kline_pattern_report.html", "w", encoding="utf-8") as f:
    f.write(html_doc)
print("report saved:", len(html_doc), "chars")
