# -*- coding: utf-8 -*-
"""Step 8: 生成 V2 报告 —— 跨周期/跨年复验 + 八卦视角"""
import json, html

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
YRS = json.load(open(f"{OUT}/results_years.json", encoding="utf-8"))
HTF = json.load(open(f"{OUT}/results_htf.json", encoding="utf-8"))
POOL = json.load(open(f"{OUT}/results_pool.json", encoding="utf-8"))
R1 = json.load(open(f"{OUT}/analysis_results.json", encoding="utf-8"))
INK, SUB, PANEL, BORDER = "#1f2329", "#646a73", "#f7f8fa", "#e3e6ea"
C_MA, C_EG = "#2456a6", "#7c3aed"
RED, GREEN = "#c0392b", "#1e8449"
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]

# 八卦映射: (code, 初爻,二爻,三爻 自下而上) -> 卦
TRIG = {
    "010000": ("乾", "☰", "天", "刚健、纯阳、亢极", "-0.0205%", -62.8, "-0.0104%", -31.1),
    "000101": ("坤", "☷", "地", "柔顺、至阴、蓄养", "+0.0034%", 5.6, "+0.0015%", 4.0),
    "000000": ("离", "☲", "火", "外实内虚、明而暗伏", "+0.0211%", 62.8, "+0.0107%", 30.5),
    "010101": ("坎", "☵", "水", "阳陷阴中、隐伏险陷", "-0.0016%", -5.3, "-0.0015%", -6.0),
    "010100": ("兑", "☱", "泽", "悦而毁折、一阴窃上", "+0.0046%", 15.2, "-0.0005%", -1.7),
    "000100": ("震", "☳", "雷", "动、一阳初生、震惊百里", "+0.0204%", 51.0, "+0.0101%", 28.2),
    "010001": ("巽", "☴", "风", "顺、入、阳伏阴下", "-0.0187%", -62.5, "-0.0087%", -29.8),
    "000001": ("艮", "☶", "山", "止、静、止于其所", "+0.0007%", 1.8, "+0.0008%", 2.4),
}
CODE2TRIG = {v[0]: k for k, v in TRIG.items()}

def get_y(sym, tag):
    for r in YRS:
        if r["sym"] == sym and r["tag"] == tag: return r
    return None

def get_h(sym, tf):
    for r in HTF["htf"]:
        if r["sym"] == sym and r["tf"] == tf: return r
    return None

def esc(s): return html.escape(str(s))

def fmt_tbl(headers, rows):
    th = "".join(f"<th>{h}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"

# ---------- SVG: 年度稳定性 ----------
def svg_years():
    W, H, pl, pt, ch, cw = 680, 280, 46, 30, 180, 600
    years = ["2020", "2021", "2022", "2023", "2024", "2025", "ALL"]
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif"><rect width="{W}" height="{H}" fill="#fff"/>']
    p.append(f'<text x="{pl}" y="16" font-size="12" font-weight="600" fill="{INK}">010000（乾·光头光脚阳线）之后下一根K线平均涨跌幅 —— 按年拆分的稳定性</text>')
    vmax = 0.028
    y0 = pt + ch / 2
    for v in (-0.03, -0.02, -0.01, 0, 0.01, 0.02, 0.03):
        y = y0 - ch / 2 * v / vmax
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+cw}" y2="{y:.1f}" stroke="{BORDER}"/>')
        p.append(f'<text x="{pl-6}" y="{y+3:.1f}" font-size="9" fill="{SUB}" text-anchor="end">{v:+.2f}%</text>')
    gw = cw / len(years)
    for i, y in enumerate(years):
        x0 = pl + i * gw
        for j, (sym, col) in enumerate((("MA", C_MA), ("EG", C_EG))):
            r = get_y(sym, y)
            v = r["st_010000"]["mean"] / 100
            bh = ch / 2 * abs(v) / vmax
            bw = gw * 0.3
            x = x0 + gw * (0.2 + j * 0.34)
            p.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}" rx="2"/>')
            t = r["st_010000"]["t"]
            p.append(f'<text x="{x+bw/2:.1f}" y="{y0+bh+12:.1f}" font-size="8.5" fill="{col}" text-anchor="middle">t={t:+.0f}</text>')
        lab = "2020-25<br/>合并" if y == "ALL" else y
        p.append(f'<text x="{x0+gw/2:.1f}" y="{pt+ch+26:.1f}" font-size="10" fill="{INK}" text-anchor="middle">{lab}</text>')
    p.append(f'<line x1="{pl}" y1="{y0}" x2="{pl+cw}" y2="{y0}" stroke="{INK}" stroke-width="1.2"/>')
    p.append(f'<text x="{pl+8}" y="{pt-8}" font-size="9.5" fill="{SUB}">注：6年×2品种=12个样本，方向全部为负（次日下跌），t值4.9~37.4全部显著</text>')
    p.append('</svg>')
    return "".join(p)

# ---------- SVG: 周期衰减 ----------
def svg_tf():
    W, H, pl, pt, ch, cw = 680, 270, 46, 34, 170, 600
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif"><rect width="{W}" height="{H}" fill="#fff"/>']
    p.append(f'<text x="{pl}" y="16" font-size="12" font-weight="600" fill="{INK}">信号幅度随K线周期衰减（甲醇，2020）——微观结构效应的尺度依赖性</text>')
    vmax = 0.055; y0 = pt + ch / 2
    for v in (-0.05, -0.025, 0, 0.025, 0.05):
        y = y0 - ch / 2 * v / vmax
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+cw}" y2="{y:.1f}" stroke="{BORDER}"/>')
        p.append(f'<text x="{pl-6}" y="{y+3:.1f}" font-size="9" fill="{SUB}" text-anchor="end">{v:+.2f}%</text>')
    series = [("1分钟", [get_y("MA", "2020"), None], C_MA), ("5分钟", [None, get_h("MA", "5m")], "#4a7fd4"), ("15分钟", [None, get_h("MA", "15m")], "#8fb3e8")]
    groups = [("010000", "乾→次日（做空信号）"), ("000100", "震→次日（做多信号）")]
    gw = cw / 3
    for gi, (s, _) in enumerate(groups):
        x0 = pl + gi * gw
        for ti, (tfname, _, col) in enumerate(series):
            if ti == 0: r = series[0][1][0]; mean = r["st_" + s]["mean"] / 100; t = r["st_" + s]["t"]; n = r["st_" + s]["n"]
            else: r = series[ti][1][1]; mean = r["st_" + s]["mean"] / 100; t = r["st_" + s]["t"]; n = r["st_" + s]["n"]
            bh = ch / 2 * abs(mean) / vmax
            bw = gw * 0.22
            x = x0 + gw * (0.18 + ti * 0.27)
            ytop = y0 if mean < 0 else y0 - bh
            p.append(f'<rect x="{x:.1f}" y="{ytop:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}" rx="2"/>')
            sig = "✓显著" if abs(t) > 3 else "✗不显著"
            scol = INK if abs(t) > 3 else SUB
            p.append(f'<text x="{x+bw/2:.1f}" y="{y0+ (bh+12 if mean<0 else -6):.1f}" font-size="8.5" fill="{scol}" text-anchor="middle">{sig}</text>')
            p.append(f'<text x="{x+bw/2:.1f}" y="{y0 + (bh+23 if mean<0 else -17):.1f}" font-size="8" fill="{SUB}" text-anchor="middle">n={n:,}</text>')
        p.append(f'<text x="{x0+gw/2:.1f}" y="{pt+ch+16:.1f}" font-size="11" fill="{INK}" text-anchor="middle">{groups[gi][1]}</text>')
    lx = pl
    for tfname, _, col in series:
        p.append(f'<rect x="{lx}" y="{H-14}" width="10" height="10" fill="{col}" rx="2"/><text x="{lx+14}" y="{H-5}" font-size="10" fill="{INK}">{tfname}</text>')
        lx += 90
    p.append('</svg>')
    return "".join(p)

# ---------- SVG: 先天八卦图 ----------
def svg_bagua():
    W, H = 680, 470
    cx, cy, Rr = 340, 235, 152
    pos = {"乾": (0, -1), "坤": (0, 1), "离": (-1, 0), "坎": (1, 0), "兑": (-1, -1), "震": (-1, 1), "巽": (1, -1), "艮": (1, 1)}
    p = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC,Microsoft YaHei,sans-serif"><rect width="{W}" height="{H}" fill="#fff"/>']
    p.append(f'<text x="16" y="22" font-size="12.5" font-weight="600" fill="{INK}">八形态即八卦：先天八卦方位图（内为下一根K线平均涨跌幅，2020-2025合并，n≈49万/品种）</text>')
    p.append(f'<text x="16" y="38" font-size="9.5" fill="{SUB}">映射规则：自下而上 初爻=下影线、二爻=实体、三爻=上影线；有影=阴爻，无影=阳爻；阳线=阳爻，阴线=阴爻</text>')
    # 对宫虚线
    for a, b in [("乾", "坤"), ("离", "坎"), ("兑", "艮"), ("震", "巽")]:
        x1, y1 = cx + pos[a][0] * Rr, cy + pos[a][1] * Rr
        x2, y2 = cx + pos[b][0] * Rr, cy + pos[b][1] * Rr
        p.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{BORDER}" stroke-dasharray="4 4"/>')
    p.append(f'<circle cx="{cx}" cy="{cy}" r="{Rr}" fill="none" stroke="{BORDER}"/>')
    p.append(f'<circle cx="{cx}" cy="{cy}" r="46" fill="{PANEL}" stroke="{BORDER}"/>')
    p.append(f'<text x="{cx}" y="{cy-4}" font-size="10" fill="{SUB}" text-anchor="middle">对宫虚线=错卦</text>')
    p.append(f'<text x="{cx}" y="{cy+10}" font-size="10" fill="{SUB}" text-anchor="middle">(完全反向K线)</text>')
    for name, (dx, dy) in pos.items():
        code = CODE2TRIG[name]
        t = TRIG[code]
        x, y = cx + dx * Rr, cy + dy * Rr
        bw, bh = 128, 74
        x0, y0 = x - bw / 2, y - bh / 2
        eff = t[4]
        col = RED if float(eff.strip('%')) > 0.004 else (GREEN if float(eff.strip('%')) < -0.004 else SUB)
        alpha = min(abs(float(eff.strip('%'))) * 40, 0.16)
        p.append(f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{bw}" height="{bh}" rx="8" fill="{col}" fill-opacity="{alpha:.3f}" stroke="{col}" stroke-width="1.2"/>')
        p.append(f'<text x="{x:.0f}" y="{y0+18:.0f}" font-size="13" font-weight="700" fill="{INK}" text-anchor="middle">{name} {t[1]}（{t[2]}）</text>')
        p.append(f'<text x="{x:.0f}" y="{y0+34:.0f}" font-size="9" fill="{SUB}" text-anchor="middle">{code} {t[3]}</text>')
        p.append(f'<text x="{x:.0f}" y="{y0+50:.0f}" font-size="10.5" font-weight="600" fill="{col}" text-anchor="middle">次日 {eff}（t={t[5]:+.0f}）</text>')
        p.append(f'<text x="{x:.0f}" y="{y0+64:.0f}" font-size="8.5" fill="{SUB}" text-anchor="middle">乙二醇 {t[6]}（t={t[7]:+.0f}）</text>')
    p.append('</svg>')
    return "".join(p)

# ---------- 表格 ----------
# T1 跨周期
t1 = []
for sym in ["MA", "EG"]:
    for tf, src in (("1分钟", get_y(sym, "2020")), ("5分钟", get_h(sym, "5m")), ("15分钟", get_h(sym, "15m"))):
        r = src
        st1, st2, st3 = r.get("st_010000"), r.get("st_000100"), r.get("st_010001")
        if "edge_short" in r:
            edge_s, edge_l = r["edge_short"], r["edge_long"]
        else:
            price = r.get("price", r.get("price_mean", 2000))
            edge_s = r["st_010000"]["mean"] / 100 * price * 10
            edge_l = r["st_000100"]["mean"] / 100 * price * 10
        t1.append([f"{'甲醇' if sym=='MA' else '乙二醇'}", tf, f"{r['n']:,}", f"{r['doji_rate']:.1%}", f"{r['mi']:.3f}", f"{r['acf1_abs']:.3f}",
                   f"{st1['mean']:+.4f}% (t={st1['t']:+.1f})" if st1 else "—",
                   f"{st2['mean']:+.4f}% (t={st2['t']:+.1f})" if st2 else "—",
                   f"{st3['mean']:+.4f}% (t={st3['t']:+.1f})" if st3 else "—",
                   f"{edge_s:+.1f} / {edge_l:+.1f}"])
T1 = fmt_tbl(["品种", "周期", "K线数", "平盘率", "互信息", "|chg|自相关", "010000→次日", "000100→次日", "010001→次日", "做空/做多边际(元/手)"], t1)

# T2 分年
t2 = []
for sym in ["MA", "EG"]:
    for y in ["2020", "2021", "2022", "2023", "2024", "2025", "ALL"]:
        r = get_y(sym, y)
        s1, s2, s3 = r["st_010000"], r["st_000100"], r["st_010001"]
        t2.append([("甲醇" if sym == "MA" else "乙二醇"), "2020-25合并" if y == "ALL" else y, f"{r['n']:,}", f"{r['doji_rate']:.1%}",
                   f"{r['mi']:.3f}", f"{r['acf1_abs']:.3f}",
                   f"{s1['mean']:+.4f}% (t={s1['t']:+.1f})", f"{s2['mean']:+.4f}% (t={s2['t']:+.1f})", f"{s3['mean']:+.4f}% (t={s3['t']:+.1f})"])
T2 = fmt_tbl(["品种", "年份", "K线数", "平盘率", "互信息", "|chg|自相关", "010000→次日", "000100→次日", "010001→次日"], t2)

# T3 八卦映射
t3 = []
for code in ["010000", "000000", "000100", "010001", "010100", "000001", "000101", "010101"]:
    t = TRIG[code]
    t3.append([f"<code>{code}</code>", f"{t[0]} {t[1]}", t[2], t[3], f"{t[4]} (t={t[5]:+.0f})", f"{t[6]} (t={t[7]:+.0f})"])
T3 = fmt_tbl(["六位编码", "卦", "象", "易理象征", "甲醇→次日均值(t)", "乙二醇→次日均值(t)"], t3)

# T4 之卦(转移) —— 取2020年MA显著转移top + 易理读法
READ = {
    ("010000", "000000"): "乾之离：中爻（实体）由阳转阴，无影外壳不变——纯阳之体自转为「外实内虚」，亢极阴生",
    ("000000", "010000"): "离之乾：中虚之阴转阳复实——「外实内虚」修复，阴尽阳回，绝地反涨",
    ("000100", "010100"): "震之兑：中爻阴体转阳体，影线不动——一阳初生而动遂（震为动，兑为悦）",
    ("010001", "000001"): "巽之艮：中爻阳体转阴体——风止为山，阳体退场",
    ("010000", "010000"): "乾之乾：八纯卦自续，纯阳延续；然方向仍以次日下跌为继（有悔）",
    ("010001", "010001"): "巽之巽：单影阳线自续，影线结构有粘性",
    ("000000", "010100"): "离之兑：中阴转阳——光脚阴次日多成带上影阳线",
    ("010000", "010100"): "乾之兑：上爻之变（仅加上影）——极少发生（lift 0.17），纯阳之体不肯只借影而变",
    ("010001", "010100"): "巽之兑：初末两爻齐变——极少发生（lift 0.17）",
    ("000100", "000001"): "震之艮：两爻齐变（实体+上影）——显著少于基准",
}
t4 = []
for x in R1["MA"]["sig_trans"][:12]:
    key = (x["from"], x["to"])
    if key not in READ: continue
    eg_z = None
    for y in R1["EG"]["sig_trans"]:
        if (y["from"], y["to"]) == key: eg_z = y
    t4.append([f"<code>{x['from']}</code>→<code>{x['to']}</code>", TRIG[x["from"]][0] + "之" + TRIG[x["to"]][0],
               f"{x['p']:.1%}", f"{x['lift']:.2f}", f"z={x['z']:+.0f}",
               (f"EG lift {eg_z['lift']:.2f}" if eg_z else "EG不显著"), READ[key]])
T4 = fmt_tbl(["转移(编码)", "之卦", "条件概率", "提升度", "显著性", "乙二醇", "易理读法（文化类比）"], t4)

# T5 错卦/综卦
t5 = [
    ["错卦（对宫，三爻全反=K线完全反向）", "乾↔坤", "光头光脚阳 ↔ 双影阴", "-0.0205% / +0.0034%", "阳极转阴、阴极阳生"],
    ["", "离↔坎", "光头光脚阴 ↔ 双影阳", "+0.0211% / -0.0016%", "外实内虚→涨；阳陷阴中→平"],
    ["", "震↔巽", "带上影阴 ↔ 带下影阳", "+0.0204% / -0.0187%", "上影阴→涨；下影阳→跌"],
    ["", "艮↔兑", "带下影阴 ↔ 带上影阳", "+0.0007% / +0.0046%", "单影两态均弱"],
    ["综卦（上下颠倒=影线换边、实体同向）", "震↔艮", "阴体：上影 ↔ 下影", "+0.0204% / +0.0007%", "同为阴体，影在上侧则次日大涨，在下侧则平"],
    ["", "巽↔兑", "阳体：下影 ↔ 上影", "-0.0187% / +0.0046%", "同为阳体，影在下侧则次日大跌，在上侧则弱涨"],
    ["", "乾/坤/坎/离 自综", "光脚/双影翻转后仍是自己", "—", "对称形态，结构不变"],
]
T5 = fmt_tbl(["结构关系", "卦对", "形态对应", "甲醇次日效应", "统计与易理互证"], t5)

# T6 经济性
pm = POOL["MA"]["price"]; pe = POOL["EG"]["price"]
e = lambda sym, s: POOL[sym]["states"][s]["mean"] / 100 * POOL[sym]["price"] * 10
T6 = fmt_tbl(["信号", "甲醇幅度(2020-25合并)", "折合每手", "乙二醇幅度", "折合每手", "往返成本", "净边际"],
    [["乾(010000)后做空", f"{POOL['MA']['states']['010000']['mean']:+.4f}%", f"{e('MA','010000'):+.1f} 元", f"{POOL['EG']['states']['010000']['mean']:+.4f}%", f"{e('EG','010000'):+.1f} 元", "约14~28元", "不足以覆盖"],
     ["震(000100)后做多", f"{POOL['MA']['states']['000100']['mean']:+.4f}%", f"{e('MA','000100'):+.1f} 元", f"{POOL['EG']['states']['000100']['mean']:+.4f}%", f"{e('EG','000100'):+.1f} 元", "约14~28元", "不足以覆盖"],
     ["巽(010001)后做空", f"{POOL['MA']['states']['010001']['mean']:+.4f}%", f"{e('MA','010001'):+.1f} 元", f"{POOL['EG']['states']['010001']['mean']:+.4f}%", f"{e('EG','010001'):+.1f} 元", "约14~28元", "不足以覆盖"]])

html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>K线编码规律复验 + 八卦视角（V2）</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#fff;color:{INK};max-width:1080px;margin:0 auto;padding:28px 20px 60px;line-height:1.75;font-size:14.5px}}
h1{{font-size:23px;border-bottom:3px solid {INK};padding-bottom:12px}}
h2{{font-size:19px;margin-top:44px;border-left:5px solid {C_MA};padding-left:10px}}
h3{{font-size:16px;margin-top:26px}}
table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:12.5px}}
th{{background:{PANEL};border:1px solid {BORDER};padding:6px 7px;text-align:left;white-space:nowrap}}
td{{border:1px solid {BORDER};padding:6px 7px}}
tr:nth-child(even) td{{background:#fafbfc}}
code{{background:{PANEL};padding:1px 5px;border-radius:3px;font-size:12px;font-family:Consolas,monospace}}
.panel{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;padding:14px 18px;margin:16px 0}}
.finding{{border:1px solid {BORDER};border-left:5px solid {C_MA};border-radius:6px;padding:12px 18px;margin:18px 0}}
.finding h3{{margin-top:0;color:{C_MA}}}
.warn{{border-left-color:#d4380d;background:#fff7f0}} .warn h3{{color:#d4380d}}
.bagua{{border-left-color:#8c6d1f;background:#fffdf5}} .bagua h3{{color:#8c6d1f}}
.small{{font-size:12.5px;color:{SUB}}}
.svgbox{{border:1px solid {BORDER};border-radius:8px;padding:10px;margin:14px 0}}
ul{{margin:8px 0 8px 4px}} li{{margin:4px 0}}
.footer{{margin-top:50px;border-top:2px solid {BORDER};padding-top:14px;font-size:12.5px;color:{SUB}}}
</style></head><body>

<h1>V2：跨周期·跨年份复验，与一个八卦视角</h1>
<div class="small">接《一分钟K线六位二进制编码转换与规律分析》（kline_pattern_report.html）。本轮完成两件事：① 按第一轮建议做样本外复验（5/15分钟线、2021-2025年）；② 应要求从八卦角度重新审视这套八态编码。数据：甲醇MA/乙二醇EG 主力连续1分钟，2020-2025共 <b>489,975根/品种</b>。</div>

<h2>一、复验一：放到5分钟/15分钟线上（2020年）</h2>
<div class="svgbox">{svg_tf()}</div>
{T1}
<div class="finding">
<h3>结论：效应有明确的「尺度」</h3>
<ul>
<li><b>波动率聚集全周期稳健</b>：|涨跌幅|自相关在1m/5m/15m分别为 0.21/0.21/0.16（甲醇）、0.27/0.25/0.21（乙二醇）——不随时问衰减，是最可靠的规律。</li>
<li><b>「无影光头线反转」是1分钟尺度的微观结构效应</b>：甲醇010000效应的t值从1分钟的 <b>-27.7</b> 衰减到5分钟的 <b>-1.1</b>（不显著）、15分钟样本太少（n=71）无法判断。插针回补主要发生在分钟之内。</li>
<li><b>影线类效应衰减较慢</b>：000100（带上影阴→涨）在5分钟仍显著（t=+6.7），15分钟仍存（t=+2.9，边际）；010001（长下影阳→跌）5分钟显著（t=-5.0），15分钟消失。</li>
<li>平盘率随时问下降（甲醇25.7%→14.5%→9.6%）：周期越大，实体方向字段越「干净」，但剔除平盘后仍≈50%公平硬币。</li>
</ul>
</div>

<h2>二、复验二：2021-2025年样本外（每年约8.3万根/品种）</h2>
<div class="svgbox">{svg_years()}</div>
{T2}
<div class="finding">
<h3>结论：三大规律跨年全部成立，不是2020年的巧合</h3>
<ul>
<li><b>12个年度样本（6年×2品种），010000→次日跌、000100→次日涨、010001→次日跌，方向全部一致且t值全部>3</b>；合并后甲醇t值达 -62.8 / +51.0 / -62.5（n≈49万），乙二醇 -31.1 / +28.2 / -29.8。</li>
<li>效应量级跨年稳定：甲醇约 -0.020% / +0.020% / -0.019%，乙二醇约 -0.010% / +0.010% / -0.009%（乙二醇效应量系统性偏小，约甲醇一半）。</li>
<li>互信息年度间有波动（甲醇0.08~0.26，2024-25明显走高），平盘率也从16.6%(2021)升到29.8%(2025)——市场微观结构在变，编码方案依旧适用，但参数需按年重估。</li>
</ul>
</div>

<h2>三、经济性再检验（合并6年）</h2>
{T6}
<p class="small">结论与第一轮一致且更稳固：规律真实、跨年稳健，但1分钟级别的信号边际（约4~5元/手）仍低于往返成本（手续费+滑点约14~28元/手）。5/15分钟上信号未增强反而衰减，<b>不存在「放大周期即可交易」的捷径</b>。定位不变：特征工程、波动率状态识别、执行风控。</p>

<h2>四、八卦视角：八态编码恰好就是一套「卦」</h2>

<div class="finding bagua">
<h3>4.1 映射规则：一根K线 = 一卦，三爻各有其位</h3>
<p>把一根K线<b>自下而上</b>读作三爻——这正是K线的时间结构（开盘在下、收盘在中、盘中上探在上）：</p>
<ul>
<li><b>初爻（下）= 下影线</b>：有下影=阴爻⚋，无下影=阳爻⚊</li>
<li><b>二爻（中）= 实体</b>：阳线=阳爻⚊，阴线=阴爻⚋</li>
<li><b>三爻（上）= 上影线</b>：有上影=阴爻⚋，无上影=阳爻⚊</li>
</ul>
<p>规则内核只有一条：<b>影线属阴（虚、试探、受阻），实体属阳（实、主体）；无影即纯实，故为阳</b>。于是八个形态与八卦形成一一对应，且两个「极端形态」的归属完全自然：<b>光头光脚阳线=乾☰（纯阳之卦），双影阴线=坤☷（至阴之卦）</b>。</p>
</div>

<div class="svgbox">{svg_bagua()}</div>
{T3}

<div class="finding bagua">
<h3>4.2 结构验证：错卦=完全反向K线，综卦=影线换边</h3>
<p>八卦的两组结构关系，恰好对应K线的两组自然运算，且<b>全部吻合</b>：</p>
{T5}
<p>这不是硬凑的：错卦（三爻全反）在K线上就是「完全反向」的一根；综卦（上下颠倒）在K线上就是「实体同向、上下影对调」。八态编码在这两组运算下<b>封闭</b>——它确实是一个群结构完备的八元素系统，与八卦同构。</p>
</div>

<div class="finding bagua">
<h3>4.3 本卦与变卦：转移矩阵就是「之卦」表</h3>
<p>六爻占断以本卦看现状、变卦看结果；这里<b>本卦=当前分钟的形态，变卦=下一分钟的形态</b>，转移矩阵即一张「之卦」表。统计上最固定的几条路径（2020年甲醇，|z|&gt;3）：</p>
{T4}
<p>两个结构性观察：<b>① 高频转移几乎都是「中爻之变」</b>——实体翻转、影线外壳保持（乾之离、离之乾、震之兑、巽之艮），即影线结构比实体方向更「粘」；② 乾之兑、巽之兑这类「体不变只借影而变」的转移显著稀罕（lift 0.17）。用易学的话说：<b>变易多生于中爻（实体），而上下爻（影线）主静</b>。</p>
</div>

<div class="finding bagua">
<h3>4.4 统计与易理的暗合点（文化类比，非占断）</h3>
<ul>
<li><b>乾（010000）→ 次日大跌</b>：乾文言「亢龙有悔，盈不可久也」——纯阳之极，阴生于中。统计：-0.0205%（t=-62.8），正是八态中最强的做空信号。<b>「光头光脚阳线不可追」这条古训，在49万根样本里被量化证实。</b></li>
<li><b>坤（000101）→ 次日弱涨</b>：至阴之后阳生，「履霜」而至「坚冰」，阴极而转。统计：+0.0034%（t=+5.6，弱但显著）。</li>
<li><b>艮（000001）→ 次日平</b>：艮止也，止于其所。统计：+0.0007%（t=+1.8，与零无异）——山卦之「止」，竟是八态中最「不动」的一个。</li>
<li><b>震（000100）→ 次日大涨</b>：震为动、「震惊百里」，一阳初生而动。统计：+0.0204%（t=+51.0）。</li>
<li><b>巽（010001）→ 次日大跌</b>：巽为风、为顺、为伏——阳伏阴下，随风而下。统计：-0.0187%（t=-62.5）。</li>
<li><b>坎（010101）→ 次日平</b>：坎为陷，阳陷阴中，陷而不起。统计：-0.0016%。</li>
</ul>
<p class="small">说明：以上是<b>文化视角的类比解读</b>。真实证据是统计显著性与跨年/跨品种稳健性；易理提供的是记忆框架与直觉命名（如「亢龙有悔=别追光脚阳」），不承担预测职能。</p>
</div>

<h2>五、总结论</h2>
<ol>
<li><b>编码方案成立</b>：8态对1分钟数据饱满，且与八卦同构（错卦=反向K线、综卦=影线换边、转移=之卦），作为描述/特征框架既紧凑又有文化纵深。</li>
<li><b>规律分层</b>：波动率聚集（全周期全年度稳健）＞影线位置效应（1分钟最强，5分钟衰减）＞无影反转（1分钟微观结构专用）。</li>
<li><b>跨样本稳健</b>：12个年度样本方向全一致；可放心作为特征使用，但别当独立策略。</li>
<li><b>下一步建议</b>：① 把「影线在上/在下」作为独立二值特征（比实体方向更有效）；② 用形态码做波动率 regime 过滤；③ 若要追求可交易信号，需与更大周期趋势/量价信息复合，并在2026年数据上滚动验证。</li>
</ol>

<div class="footer">
<p><b>本轮新增文件</b>（E:/抖音分析/2026-10-16-19-50-44/kline_encoding/）：MA/EG_2021_2025_1m_raw.csv、MA/EG_2020_5m/15m_encoded_raw.csv、results_years.json、results_htf.json、results_pool.json、step5~step8脚本、本报告 kline_pattern_report_v2.html。</p>
<p class="small">八卦内容为传统文化视角的类比框架，仅供文化参考；统计结论基于历史数据，不构成投资建议。</p>
</div>
</body></html>"""

with open(f"{OUT}/kline_pattern_report_v2.html", "w", encoding="utf-8") as f:
    f.write(html_doc)
print("v2 report saved:", len(html_doc), "chars")
