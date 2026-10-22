# -*- coding: utf-8 -*-
"""step19: 生成编码优化方案报告 report_v5.html"""
import json, numpy as np

OUT = "."
O = json.load(open(f'{OUT}/results_optimize.json', encoding='utf-8'))
A = json.load(open(f'{OUT}/results_optimize_add.json', encoding='utf-8'))

INK, SUB, LINE = '#26215C', '#5F5E5A', '#D3D1C7'
RED, GREEN = '#D7453B', '#1E9E6A'
BLUE, TEAL, PURP, AMBER = '#3B6FD4', '#0E8A8A', '#7C5CBF', '#C77D1A'

def f(x, d=4): return f"{x:.{d}f}"

# ============ 图1: 位分配对比 ============
def fig_bits():
    p = []
    p.append(f'<text x="40" y="26" font-size="14" font-weight="600" fill="{INK}">同一个字节/六位里，各自装了多少真实信息</text>')
    # 原6位码: 3段x2bit, 每段只用一半
    p.append(f'<text x="40" y="54" font-size="12" fill="{SUB}">原编码：6 位载体 = 3 段×2 位，每段只有 00/01 两种状态（另一半状态从未使用）</text>')
    segs = [('实体', '跌/涨', 2), ('上影', '无/有', 2), ('下影', '无/有', 2)]
    x = 40
    for name, val, w in segs:
        p.append(f'<rect x="{x}" y="66" width="{w*46}" height="40" rx="4" fill="#F0EEE6" stroke="{LINE}" stroke-width="1"/>')
        p.append(f'<text x="{x+w*23}" y="82" font-size="10.5" font-weight="600" fill="{SUB}" text-anchor="middle">{name}</text>')
        p.append(f'<text x="{x+w*23}" y="98" font-size="10" fill="{SUB}" text-anchor="middle">{val}</text>')
        # 浪费的一半
        p.append(f'<rect x="{x+w*46-23}" y="66" width="23" height="40" fill="{LINE}" fill-opacity="0.35"/>')
        p.append(f'<text x="{x+w*46-11.5}" y="90" font-size="8.5" fill="#8A8880" text-anchor="middle">废</text>')
        x += w*46 + 10
    p.append(f'<text x="{x+6}" y="82" font-size="11" fill="{SUB}">有效 3 bit</text>')
    p.append(f'<text x="{x+6}" y="98" font-size="11" font-weight="700" fill="{RED}">熵 2.93 bit</text>')
    # code7
    y2 = 150
    p.append(f'<text x="40" y="{y2-8}" font-size="12" fill="{SUB}">优化编码 code7：7 位全用满，实体升为三态（跌/平/涨），下影+上影合并为 CLV 8 档，再送 2 位振幅档</text>')
    bits = [('实体3态', '跌/平/涨', 2, BLUE), ('CLV 8档', '收盘位置', 3, TEAL), ('振幅4档', '波动水平', 2, AMBER)]
    x = 40
    for name, val, w, col in bits:
        p.append(f'<rect x="{x}" y="{y2+4}" width="{w*46}" height="40" rx="4" fill="{col}" fill-opacity="0.14" stroke="{col}" stroke-width="1.2"/>')
        p.append(f'<text x="{x+w*23}" y="{y2+20}" font-size="10.5" font-weight="600" fill="{col}" text-anchor="middle">{name}</text>')
        p.append(f'<text x="{x+w*23}" y="{y2+36}" font-size="10" fill="{SUB}" text-anchor="middle">{val}</text>')
        x += w*46 + 10
    p.append(f'<text x="{x+6}" y="{y2+20}" font-size="11" fill="{SUB}">有效 7 bit</text>')
    p.append(f'<text x="{x+6}" y="{y2+36}" font-size="11" font-weight="700" fill="{GREEN}">熵 5.07 bit</text>')
    # 底部注释
    p.append(f'<line x1="40" y1="224" x2="640" y2="224" stroke="{LINE}" stroke-width="0.5"/>')
    p.append(f'<text x="40" y="244" font-size="11" fill="{SUB}">关键差别不在位数，在信息密度：CLV 一个数同时编码「上影有无与长短、下影有无与长短、收盘强弱」，</text>')
    p.append(f'<text x="40" y="262" font-size="11" fill="{SUB}">且保留了原码丢失的影线长度；三值实体把甲醇 25.7% 的平盘K线从「涨」里拆出来单独成态。</text>')
    return f'<svg viewBox="0 0 680 280" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">{"".join(p)}</svg>'

# ============ 图2: 等比特互信息对比 ============
def fig_mi():
    feats = [('原码8态', 'old8', '#B4B2A9'), ('CLV 8档', 'clv8', TEAL), ('CLV 16档', 'clv16', BLUE), ('新字节码', 'newbyte', PURP)]
    tgts = [('MI_nextcode', '下一根的形态（8态）'), ('MI_nextamp', '下一根的振幅（4分档）'), ('MI_nextdir', '下一根的方向（4分档）')]
    p = [f'<text x="40" y="24" font-size="14" font-weight="600" fill="{INK}">等比特预算下的预测力：CLV 单独一项就超过整个原码</text>',
         f'<text x="40" y="44" font-size="11.5" fill="{SUB}">2020-2025 全样本 484,255 个相邻分钟对；互信息越高=该特征对下一根K线的「了解」越多</text>']
    for pi, sym in enumerate(['MA', 'EG']):
        x0 = 40 + pi * 316
        maxv = max(A[sym][t][fk] for t, _ in tgts for _, fk, _ in feats) * 1.15
        # 图例
        for fi, (fn, fk, col) in enumerate(feats):
            p.append(f'<rect x="{x0+fi*76}" y="58" width="10" height="10" fill="{col}"/>')
            p.append(f'<text x="{x0+fi*76+14}" y="67" font-size="9.5" fill="{SUB}">{fn}</text>')
        for gi, (tk, tname) in enumerate(tgts):
            gy = 84 + gi * 74
            p.append(f'<text x="{x0}" y="{gy+10}" font-size="10.5" font-weight="600" fill="{INK}">{tname}</text>')
            for fi, (fn, fk, col) in enumerate(feats):
                v = A[sym][tk][fk]
                bw = 56
                bx = x0 + fi * 66
                bh = max(v / maxv * 44, 1.2)
                p.append(f'<rect x="{bx}" y="{gy+50-bh:.1f}" width="{bw}" height="{bh:.1f}" rx="2" fill="{col}"/>')
                p.append(f'<text x="{bx+bw/2}" y="{gy+50-bh-4:.1f}" font-size="9" font-weight="600" fill="{col}" text-anchor="middle">{v:.3f}</text>')
            p.append(f'<line x1="{x0}" y1="{gy+50}" x2="{x0+272}" y2="{gy+50}" stroke="{LINE}" stroke-width="0.5"/>')
        p.append(f'<text x="{x0}" y="{84+3*74+16}" font-size="11" font-weight="700" fill="{INK}">{"甲醇 MA" if sym=="MA" else "乙二醇 EG"}</text>')
    p.append(f'<text x="40" y="330" font-size="11" fill="{SUB}">读法：三比特的 CLV（8档）在三个目标上全部压过三比特的原码；四比特 CLV（16档）再上一层；新字节码把振幅档纳入后优势最大。</text>')
    return f'<svg viewBox="0 0 680 348" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">{"".join(p)}</svg>'

# ============ 图3: CLV 几何 ============
def fig_clv():
    p = [f'<text x="40" y="26" font-size="14" font-weight="600" fill="{INK}">为什么 CLV 一个数能换掉两个影线字段</text>']
    def candle(cx, o, c, h, l, col, label, note):
        top, bot = max(o, c), min(o, c)
        yh, yl = 60, 240
        def Y(v): return yh + (h - v) / (h - l) * (yl - yh)
        out = []
        out.append(f'<line x1="{cx}" y1="{Y(h)}" x2="{cx}" y2="{Y(l)}" stroke="{col}" stroke-width="1.6"/>')
        out.append(f'<rect x="{cx-16}" y="{Y(top)}" width="32" height="{max(Y(bot)-Y(top),1.5)}" fill="{col}"/>')
        out.append(f'<text x="{cx}" y="{Y(h)-8}" font-size="9.5" fill="{SUB}" text-anchor="middle">high</text>')
        out.append(f'<text x="{cx}" y="{Y(l)+16}" font-size="9.5" fill="{SUB}" text-anchor="middle">low</text>')
        out.append(f'<text x="{cx+52}" y="{Y(o)+4}" font-size="9.5" fill="{SUB}">open</text>')
        out.append(f'<text x="{cx+52}" y="{Y(c)+4}" font-size="9.5" fill="{SUB}">close</text>')
        # CLV 标尺
        clv = (c - l) / (h - l)
        yc_ = Y(c)
        out.append(f'<line x1="{cx-16}" y1="{yc_}" x2="{cx+40}" y2="{yc_}" stroke="{PURP}" stroke-width="1" stroke-dasharray="3,2"/>')
        out.append(f'<text x="{cx-20}" y="{yc_+4}" font-size="9.5" font-weight="700" fill="{PURP}" text-anchor="end">CLV={clv:.2f}</text>')
        out.append(f'<text x="{cx}" y="262" font-size="11" font-weight="600" fill="{INK}" text-anchor="middle">{label}</text>')
        out.append(f'<text x="{cx}" y="280" font-size="9.5" fill="{SUB}" text-anchor="middle">{note}</text>')
        return out
    p += candle(120, 100, 106, 112, 96, RED, '阳线 + 长上影', 'CLV=0.63 → 上影/range=0.31')
    p += candle(300, 106, 100, 108, 98, GREEN, '阴线 + 长下影', 'CLV=0.25 → 下影/range=0.50')
    p += candle(480, 100, 112, 112, 100, RED, '光头阳线', 'CLV=1.00 → 无上影')
    p += candle(600, 112, 100, 112, 100, GREEN, '光脚阴线', 'CLV=0.00 → 无下影')
    p.append(f'<line x1="40" y1="300" x2="640" y2="300" stroke="{LINE}" stroke-width="0.5"/>')
    p.append(f'<text x="40" y="320" font-size="11.5" fill="{SUB}">恒等式（β=涨跌幅/振幅，即实体占振幅比）：</text>')
    p.append(f'<text x="40" y="340" font-size="12" font-weight="600" fill="{INK}" font-family="Consolas,monospace">上影/range = 1 − CLV + min(β,0)　　下影/range = CLV − max(β,0)</text>')
    p.append(f'<text x="40" y="362" font-size="11" fill="{SUB}">影线「有无与长短」被 (chg, amp, CLV) 完全决定 —— 已在 48 万根上逐根验证，准确率 100%。</text>')
    return f'<svg viewBox="0 0 680 380" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">{"".join(p)}</svg>'

# ============ 图4: 量化误差曲线 ============
def fig_quant():
    rows = O['MA']['quant_bits']
    p = [f'<text x="40" y="24" font-size="14" font-weight="600" fill="{INK}">若要压成定长比特：多少位才够（甲醇，误差为价格百分点 bp）</text>']
    x0, y0, cw, ch = 90, 60, 520, 200
    maxe = 3.2
    def X(b): return x0 + (b - 3) / 9 * cw
    def Y(e): return y0 + ch - e / maxe * ch
    for gv in [0.5, 1, 2, 3]:
        p.append(f'<line x1="{x0}" y1="{Y(gv):.1f}" x2="{x0+cw}" y2="{Y(gv):.1f}" stroke="{LINE}" stroke-width="0.5"/>')
        p.append(f'<text x="{x0-8}" y="{Y(gv)+4:.1f}" font-size="10" fill="{SUB}" text-anchor="end">{gv} bp</text>')
    # chg 线 (bits_chg), amp 线 (bits_amp)
    for key, col, nm, off in [('mae_chg_pct', RED, '涨跌幅 chg', 0), ('mae_amp_pct', BLUE, '振幅 amp', 0)]:
        pts = []
        for r in rows:
            bx = X(r['bits_chg'] if key == 'mae_chg_pct' else r['bits_amp'])
            by = Y(r[key] * 100)
            pts.append(f'{bx:.1f},{by:.1f}')
        p.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="2"/>')
        for r in rows:
            bx = X(r['bits_chg'] if key == 'mae_chg_pct' else r['bits_amp'])
            by = Y(r[key] * 100)
            p.append(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="3.5" fill="{col}"/>')
        p.append(f'<text x="{pts and float(pts[-1].split(",")[0])+6}" y="{float(pts[-1].split(",")[1])+4}" font-size="11" font-weight="600" fill="{col}">{nm}</text>')
    p.append(f'<text x="{x0+cw/2}" y="{y0+ch+22}" font-size="10.5" fill="{SUB}" text-anchor="middle">每根K线用于该字段的比特数（chg 与 amp 独立配置）</text>')
    p.append(f'<text x="40" y="{y0+ch+48}" font-size="11" fill="{SUB}">甜点：chg 8 位 + amp 6 位 + CLV 4 位 ≈ 18 bit/根，平均误差 0.25 / 0.63 bp——不到最小变动价位的万分之一。</text>')
    return f'<svg viewBox="0 0 680 330" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">{"".join(p)}</svg>'

# ============ 表格 ============
def tbl(headers, rows, cls=''):
    h = ''.join(f'<th>{x}</th>' for x in headers)
    b = ''.join('<tr>' + ''.join(f'<td>{x}</td>' for x in r) + '</tr>' for r in rows)
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'

# T1 方案对比
t1 = tbl(['方案', '字段/根', '载体', '有效信息', '保真度', '预测力 MI(下一形态)', '定位'],
    [['<b>原编码</b>', '4（六位码+open+chg+amp）', '6位字符串', '3 bit（熵2.93）', '有损：影线丢长度、平盘归涨', f"{f(A['MA']['MI_nextcode']['old8'],3)} bit", '已被全面超越'],
     ['<b>A 无损四元组</b>', '4（open+chg+amp+CLV）', '4个float', '全量', '<b>无损</b>（双射，误差=0）', f"≥{f(A['MA']['MI_nextcode']['clv16'],3)} bit", '<b>默认存储格式</b>'],
     ['<b>B 单字节离散码</b>', '1（code7）', '1字节', '7 bit（熵5.07，53~75态）', '近似（量化）', f"{f(A['MA']['MI_nextcode']['newbyte'],3)} bit", '马尔可夫/频率/互信息分析'],
     ['<b>C 纯形状三元组</b>', '3（chg+amp+CLV）', '3个float', '全量形状', '形状无损（不含价位）', '同A', '跨品种/跨期形态比对'],
     ['<b>C+ 链式存储</b>', '平均3（+会话首open）', '链式', '全量', '无损（gap旗标补跳空）', '同A', '长期归档，体积最小']])

# T2 位定义
t2 = tbl(['位段', '位数', '编码', '说明'],
    [['实体 body3', '2', '00=跌 / 01=平 / 10=涨', '平盘单独成态——甲醇25.7%、乙二醇16.9%的K线不再污染方向统计'],
     ['CLV 档', '3', '000~111 = 收盘位置8等分', '(close−low)/(high−low)；同时决定上下影有无与长短'],
     ['振幅档', '2', '00~03 = 振幅四分位', '阈值按品种固定（MA: 0.090/0.115/0.170%；EG: 0.064/0.093/0.136%）']])

# T3 MI 全表
t3rows = []
for sym in ['MA', 'EG']:
    for tk, tn in [('MI_nextcode', '下一根形态'), ('MI_nextamp', '下一根振幅'), ('MI_nextdir', '下一根方向')]:
        r = A[sym][tk]
        t3rows.append([f'<b>{sym}</b>' if tk == 'MI_nextcode' else '', tn,
                       f(r['old8']), f(r['clv8']), f(r['clv16']), f(r['body3']), f(r['newbyte'])])
t3 = tbl(['品种', '预测目标', '原码8态', 'CLV 8档', 'CLV 16档', '实体3态', '新字节码'], t3rows)

# T4 三值实体
t4rows = []
for sym in ['MA', 'EG']:
    tr = O[sym]['ternary']
    for k, kn in [('prev_down', '前根=跌'), ('prev_flat', '前根=平'), ('prev_up', '前根=涨')]:
        v = tr[k]
        t4rows.append([f'<b>{sym}</b>' if k == 'prev_down' else '', kn, f"{v['n']:,}",
                       f"{v['p_next_up']*100:.1f}%", f"{v['p_next_dn']*100:.1f}%", f"{v['p_next_flat']*100:.1f}%",
                       f"{v['mean_next_chg']:+.4f}%", f"{v['t']:+.1f}"])
t4 = tbl(['品种', '前根实体', '样本数', '下根涨', '下根跌', '下根平', '下根均值', 't值'], t4rows)

# T5 量化甜点
t5rows = []
for r in O['MA']['quant_bits']:
    t5rows.append([f"{r['bits_chg']} + {r['bits_amp']}", f"{r['mae_chg_pct']*100:.2f} bp", f"{r['mae_amp_pct']*100:.2f} bp",
                   f"{r['bits_chg']+r['bits_amp']+4} bit（含CLV4位）"])
t5 = tbl(['chg位 + amp位', 'chg平均误差', 'amp平均误差', '每根合计'], t5rows)

# ============ 组装 ============
css = """
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#FBFAF7;color:#26215C;margin:0;padding:32px;line-height:1.65}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:36px 0 12px;border-left:4px solid #3B6FD4;padding-left:10px}
.sub{color:#5F5E5A;font-size:13px;margin-bottom:24px}
.box{background:#fff;border:1px solid #E4E2D9;border-radius:10px;padding:16px 18px;margin:14px 0}
.box.ok{border-color:#BFE3D0;background:#F4FBF7}
.box.warn{border-color:#F2D9A8;background:#FDF9EF}
.box.info{border-color:#C9D8F2;background:#F5F8FE}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:12px 0}
th{background:#F0EEE6;text-align:left;padding:7px 9px;border:1px solid #E4E2D9;font-weight:600}
td{padding:7px 9px;border:1px solid #E4E2D9;vertical-align:top}
code{background:#F0EEE6;padding:1px 5px;border-radius:4px;font-size:12px;font-family:Consolas,monospace}
.fig{background:#fff;border:1px solid #E4E2D9;border-radius:10px;padding:14px;margin:14px 0}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.pill.pass{background:#E3F4EC;color:#14714B}.pill.p-fail{background:#FBE7E5;color:#A8322A}
ul{margin:8px 0;padding-left:22px}li{margin:4px 0}
.small{font-size:12px;color:#5F5E5A}
.formula{font-family:Consolas,monospace;background:#F7F6F1;border:1px solid #E4E2D9;border-radius:8px;padding:10px 14px;font-size:13px;margin:10px 0}
"""

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>K线编码优化方案</title><style>{css}</style></head><body><div class="wrap">
<h1>六位编码的优化方案：更小维度、更多信息、照样能分析</h1>
<div class="sub">甲醇 MA / 乙二醇 EG · 2020-2025 全样本（各 489,975 根 1 分钟线，484,255 个相邻分钟对）· 2026-10-17</div>

<div class="box info"><b>一句话结论</b>：把「上影有无 + 下影有无」两个字段合并成<b>一个 CLV（收盘位置）</b>，实体升为<b>三值（跌/平/涨）</b>，再送<b>2 位振幅档</b>——得到 7 位的 <code>code7</code>：信息量从 3 bit 升到 5.1 bit，预测力全面超过原码，且影线从「只记有无」变成「有无+长短全保留」。若求无损，<code>(open, chg, amp, CLV)</code> 四元组与 OHLC 是<b>数学双射</b>（round-trip 误差 = 0）。</div>

<h2>一、原编码的四个浪费</h2>
<ul>
<li><b>一半载体空转</b>：每段 2 位只用了 00/01 两种状态，另外两种状态从未出现——6 位载体只承载 3 bit（实测熵 2.93）。</li>
<li><b>影线只记有无、不记长短</b>：长上影与秃头小影同码，而 V1~V4 的反复验证显示影线<b>长度</b>才是信息大头（预测力主驱动是 CLV，一个连续量）。</li>
<li><b>平盘被塞进「涨」</b>：<code>c&gt;=o → '01'</code> 把甲醇 25.7% 的开盘=收盘K线算作上涨，稀释了方向统计；拆成三值后反转信号立刻干净（见第四节）。</li>
<li><b>open 字段几乎冗余</b>：连续分钟内 open<sub>t</sub> = close<sub>t−1</sub> <b>100% 成立</b>（甲醇 484,255/484,255 零例外；乙二醇 99.99%，仅 38 处夜盘 21:01 断链）——绝对起始位不必每根都存。</li>
</ul>

<h2>二、核心数学：一个 CLV 换掉两个影线字段</h2>
<div class="box">定义 <b>CLV</b>（Close Location Value，收盘位置）= <code>(close − low) / (high − low)</code>，取值 0~1。
配合涨跌幅 chg 与振幅 amp，令 <b>β = chg/amp</b>（实体占振幅比，有符号），有恒等式：</div>
<div class="formula">上影/振幅 = 1 − CLV + min(β, 0)<br>下影/振幅 = CLV − max(β, 0)<br>low = close − CLV×振幅×open　　high = low + 振幅×open</div>
<div class="box ok"><b>已验证（48 万根逐根）</b>：① 由 (chg, amp, CLV) 推断上下影「有无」，准确率 <b>100.00%</b>；② 由 (open, chg, amp, CLV) 重建 (O,H,L,C)，round-trip 误差 <b>0.00e+00</b>——即四元组与 OHLC 是<b>无损双射</b>。原六位码的全部信息是这四元组的确定性函数，反过来不成立。</div>
<div class="fig">{fig_clv()}</div>

<h2>三、优化方案：三个层级，按需取用</h2>
{t1}
<div class="small">注：MI 为对「下一根形态」的互信息（比特），MA 全样本；CLV 连续版的预测力 ≥ 其 16 档量化版。方案 C 不含绝对价位，适合跨品种/跨期比对形状；要还原K线时用方案 A。</div>

<h2>四、新离散码 code7 的位定义</h2>
{t2}
<div class="fig">{fig_bits()}</div>

<h2>五、证据：等比特预算下，CLV 全面胜出</h2>
<div class="box">最公平的比较是<b>给同样的比特数</b>：原码有效 3 bit vs CLV 量化到 8 档（正好 3 bit）。结果——CLV 在<b>每一个</b>预测目标上都赢：</div>
{t3}
<div class="fig">{fig_mi()}</div>
<div class="box ok"><b>三值实体的红利</b>：把平盘从「涨」里拆出来后，反转信号从模糊变清晰（下表）。前根收涨 → 下根<b>跌 46.1% vs 涨 31.6%</b>（差距 14.5 个百分点，t=−53.0）；前根平盘 → 下根涨跌几乎均等（36.1% vs 36.6%），<b>平盘是真正的中态，不是「弱涨」</b>。原编码把这两类混为一谈，正是方向位预测力被稀释的原因之一。</div>
{t4}

<h2>六、若要压成定长比特</h2>
<div class="box">连续字段量化到定长的误差曲线（甲醇；|chg| 的 99.9 分位 = 0.580%，amp 的 99.9 分位 = 0.805%）：</div>
{t5}
<div class="fig">{fig_quant()}</div>
<div class="box warn"><b>诚实边界</b>：① CLV 在振幅=0 的K线（甲醇 0.71%、乙二醇 0.80%）上无定义，统一置 0.5 并用 body3=01 标记——双射不受影响（振幅为 0 时 high=low=close，与 CLV 取值无关）；② code7 的振幅档阈值由样本分位生成，跨品种/跨年份复用须固定阈值表（已随文件交付 <code>v2_code_thresholds.json</code>）；③ 链式存储需要 gap 旗标处理会话首根与罕见断链（乙二醇 38 处）。</div>

<h2>七、落地：文件已按新格式生成</h2>
<ul>
<li><code>MA_2020_1m_encoded_v2.csv</code> / <code>EG_2020_1m_encoded_v2.csv</code> — 方案 A+C 合体：<code>datetime, open, chg_pct, amp_pct, clv, code7, up_shadow_pct, lo_shadow_pct</code>。前五列即无损四元组；<code>code7</code> 为离散码；后两列为便利派生列（可用前五列按第二节恒等式推出，分析时不必存）。文件级重建误差 3.7e-5 / 6.5e-5 价格单位，远小于最小变动价位（1 元）。</li>
<li><code>v2_code_thresholds.json</code> — 各品种振幅四分位阈值，保证 code7 可复现。</li>
<li>复算脚本：<code>step17_optimize.py</code>（全部验证）、<code>step18_gen_v2.py</code>（文件生成）；结果 JSON：<code>results_optimize.json</code> / <code>optimize_add.json</code>。</li>
</ul>

<h2>八、与既有结论的衔接</h2>
<ul>
<li><b>regime 判断更强了</b>：code7 对「下一根振幅」的 MI 达 0.20~0.24 bit，是原码（0.028~0.037）的 6~7 倍——V4 报告里「形态码是波动率状态码」的结论在新编码下显著增强。</li>
<li><b>方向判断仍然平庸</b>：新编码把方向 MI 从 0.031 提到 0.060 bit（MA），但换算成收益仍是 0.01~0.02% 量级，跨不过 14~28 元/手的成本线——优化编码改变的是<b>信息效率与保真度</b>，不改变 1 分钟方向边际被微观摩擦锁死这一事实。</li>
<li><b>三值实体应回填到旧分析</b>：V1~V4 报告中所有「剔除平盘后」的严格方向统计，在三值编码里是原生字段，不再需要事后过滤。</li>
</ul>
</div></body></html>"""

open(f'{OUT}/kline_encoding_optimized.html', 'w', encoding='utf-8').write(html)
print('chars:', len(html), '| svg:', html.count('<svg'), html.count('</svg>'), '| tables:', html.count('<table'), html.count('</table>'))
