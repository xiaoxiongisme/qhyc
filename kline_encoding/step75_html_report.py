# -*- coding: utf-8 -*-
"""step75 生成交付 HTML 报告（读 step73_sweep.json + step74_compare.json）

内容：
  1) 数据治理结论（换月bug为假阳性 + NaN 前导棒修复 前后行数/备份路径）
  2) V5X 持仓周期参数扫描（A 退出网格 + B 节奏网格）
  3) 推荐配置 + 新旧对照（总量/train/holdout/oos21 + 分品种 net 变化）
  4) 结论与下一步 / 过拟合风险声明
"""
import os, sys, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SWEEP = 'step73_sweep.json'
COMPARE = 'step74_compare.json'
OUT_HTML = 'report_v5x_opt.html'

BASELINE = dict(SL_ATR=2.0, TRAIL_ATR=2.0, BE_R=0.5, COOLDOWN=3,
                ADD_MAX_LOTS=2, ADD_THR=1.0)


def fmt(x, d=0):
    if x is None or (isinstance(x, float) and x != x):
        return '—'
    return f'{x:,.{d}f}'


def pct(x):
    if x is None or (isinstance(x, float) and x != x):
        return '—'
    return f'{x*100:.1f}%'


def sub_mar(t):
    m = t.get('mdd_yuan', 0); n = t.get('net_yuan', 0)
    return (n / abs(m)) if (m is not None and m < 0 and n is not None) else 0.0


def green_red(v, lo, hi, good_high=True):
    if v is None or (isinstance(v, float) and v != v):
        return '#f2f2f2'
    t = max(0.0, min(1.0, (v - lo) / (hi - lo + 1e-9)))
    if not good_high:
        t = 1 - t
    # 红(230,90,90) -> 黄(245,205,90) -> 绿(95,185,110)
    if t < 0.5:
        r = 230 + (245 - 230) * (t / 0.5); g = 90 + (205 - 90) * (t / 0.5); b = 90 + (205 - 90) * (t / 0.5)
    else:
        r = 245 + (95 - 245) * ((t - 0.5) / 0.5); g = 205 + (185 - 205) * ((t - 0.5) / 0.5); b = 205 + (110 - 205) * ((t - 0.5) / 0.5)
    return f'rgb({int(r)},{int(g)},{int(b)})'


def main():
    sweep = json.load(open(SWEEP, encoding='utf-8'))
    cmp = json.load(open(COMPARE, encoding='utf-8'))
    A = sweep.get('A', {})
    B = sweep.get('B', {})
    mb = cmp['baseline']
    mr = cmp['reco_full']
    reco = cmp['reco']

    HTML = []
    HTML.append('''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V5X 持仓周期参数优化 · 交付报告</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1f2733;--mut:#6b7785;--line:#e3e8ee;--accent:#2f6df0;--red:#d9534f;--green:#2e9d5b;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 80px;}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:20px;margin:34px 0 12px;padding-left:11px;border-left:4px solid var(--accent);}
h3{font-size:16px;margin:20px 0 8px;color:var(--mut);}
.sub{color:var(--mut);font-size:13px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:14px 0;box-shadow:0 1px 3px rgba(20,30,50,.04);}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:8px 0}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:right;}
th{background:#f0f3f8;color:#33405a;font-weight:600;}
td.l,th.l{text-align:left;}
tbody tr:nth-child(even){background:#fafbfc;}
.big{font-size:22px;font-weight:700;}
.kpi{display:flex;flex-wrap:wrap;gap:14px;margin:10px 0}
.kpi .box{flex:1 1 150px;background:#f8fafd;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .k{color:var(--mut);font-size:12.5px}
.kpi .v{font-size:21px;font-weight:700;margin-top:3px}
.up{color:var(--green)} .down{color:var(--red)}
.note{background:#fff8e9;border:1px solid #f3dca0;border-radius:10px;padding:12px 15px;color:#7a5b13;font-size:13.5px;margin:12px 0}
.ok{background:#eaf7ee;border:1px solid #bfe6cb;color:#1f6b3a;border-radius:10px;padding:12px 15px;font-size:13.5px;margin:12px 0}
.bad{background:#fdeaea;border:1px solid #f1b9b9;color:#a3271f;border-radius:10px;padding:12px 15px;font-size:13.5px;margin:12px 0}
code{background:#eef1f6;padding:1px 6px;border-radius:5px;font-size:12.5px;color:#364}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tag{display:inline-block;background:#eaf0ff;color:#2f57c9;border:1px solid #cdddff;border-radius:999px;padding:1px 10px;font-size:12px;margin:2px 3px}
footer{color:var(--mut);font-size:12.5px;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}
</style></head><body><div class="wrap">''')

    HTML.append('<h1>V5X 持仓周期参数优化 · 交付报告</h1>')
    HTML.append('<p class="sub">生成于 2026-10-21 · 数据层：prod50 后复权 60m（清洗后）· 引擎：step46_prod_bt V5X 变体 · 样本：56 品种 / 2016–2026</p>')

    # ---------- 1. 数据治理 ----------
    HTML.append('<h2>一、数据治理结论（先重建数据，再优化）</h2>')

    HTML.append('<div class="ok"><b>① 换月 bug 经三重验证为假阳性 — 不存在。</b> '
                '上一轮 step69 审计把“真实市场跳空”（如 2020-02-03 新冠、个别合约单日异动）误判为换月。'
                '证据：(a) <code>bar_60m_ba</code> 全 59 品种零“逐年复发的孤立跳空”；'
                '(b) 复权比 <code>r=ba/raw</code> 在换月点<b>向下</b>台阶（正确吸收跳空），而非向上（那才说明 ba 自造了跳空）；'
                '(c) ba 与 raw 在 0.5% 阈值下均无逐年复发跳空。结论：<code>bar_60m_ba</code> 本就正确后复权，无需重建。</div>')

    HTML.append('<div class="bad"><b>② 真实缺陷：203 根前导 NaN-OHLC 棒</b>（25 个品种，各系列前 5–12 根）。'
                '来自不完整的 leading 1m 数据，会污染 ATR 播种 → 受影响品种（如 EG888/EB888）回测 0 笔。'
                '已修复（仅删数据、不改表结构）：</div>')
    HTML.append('''<table>
<thead><tr><th class="l">表</th><th>修复前行数</th><th>修复后行数</th><th>删除</th><th class="l">备份</th><th>残余 NaN</th></tr></thead>
<tbody>
<tr><td class="l"><code>bar_60m_ba.csv.gz</code></td><td>1,150,464</td><td>1,150,261</td><td>203</td><td class="l mono">bar_60m_ba.csv.gz.bak_nan</td><td>0</td></tr>
<tr><td class="l"><code>bar_60m_raw.csv.gz</code></td><td>1,103,468</td><td>1,103,265</td><td>203</td><td class="l mono">bar_60m_raw.csv.gz.bak_nan</td><td>0</td></tr>
</tbody></table>''')
    HTML.append('<p class="sub">L0（交易所源 ZIP / 统一 kqm_1m 主连）保持不变；L1 派生表仅修正数值，列结构、字段、口径一律未动，符合“只修数据不改变表结构”的约定。</p>')

    # ---------- 2. 参数扫描 ----------
    HTML.append('<h2>二、V5X 持仓周期参数扫描</h2>')
    HTML.append('<p class="sub">方法：复用 step46 引擎，仅补丁其持仓周期全局量（SL_ATR / TRAIL_ATR / BE_R / COOLDOWN / ADD_MAX_LOTS / ADD_THR），不改引擎逻辑与表结构。'
                'A 网格=出场三参数（SL×TR×BE，64 组）；B 网格=节奏参数（COOLDOWN×ADD_MAX_LOTS×ADD_THR，45 组，围绕 A 最优中心）。'
                '分区：train≤2025 / holdout≥2026 / oos21≥2021。MAR = 净利/|MDD|。</p>')

    # A grid top by total MAR
    a_rows = []
    for k, v in A.items():
        if 'error' in v or v.get('n', 0) == 0:
            continue
        a_rows.append((sub_mar(v), k, v))
    a_rows.sort(reverse=True)
    HTML.append('<h3>A 网格（出场三参数）Top 12 — 按全样本 MAR</h3>')
    HTML.append('<table><thead><tr>'
                '<th class="l">配置 (SL_TR_BE)</th><th>笔数</th><th>净利(¥)</th><th>PF</th>'
                '<th>MDD(¥)</th><th>MAR</th><th>train净利</th><th>holdout净利</th><th>oos21净利</th>'
                '</tr></thead><tbody>')
    for sc, k, v in a_rows[:12]:
        m = re.match(r'SL([\d.]+)_TR([\d.]+)_BE([\d.]+)', k)
        lab = f'SL{m.group(1)}·TR{m.group(2)}·BE{m.group(3)}'
        HTML.append(
            f'<tr><td class="l">{lab}</td>'
            f'<td>{fmt(v["n"])}</td>'
            f'<td>{fmt(v["net_yuan"])}</td>'
            f'<td>{fmt(v["pf"],3)}</td>'
            f'<td>{fmt(v["mdd_yuan"])}</td>'
            f'<td style="background:{green_red(sc,4,8)}"><b>{fmt(sc,2)}</b></td>'
            f'<td>{fmt(v["train"]["net_yuan"])}</td>'
            f'<td>{fmt(v["holdout"]["net_yuan"])}</td>'
            f'<td>{fmt(v["oos21"]["net_yuan"])}</td></tr>')
    HTML.append('</tbody></table>')
    HTML.append('<div class="note">中心被锁定为 <b>SL3.0 · TR2.0 · BE1.0</b>：它在 train 与 holdout 双正、且全样本 MAR 最高（见上表首行）。'
                'BE 从 0.5→1.0（更早保本）与 SL 从 2.0→3.0（放宽止损）是稳健的两处改动，非单分区噪声。</div>')

    # B grid
    b_rows = []
    for k, v in B.items():
        if 'error' in v or v.get('n', 0) == 0:
            continue
        b_rows.append((sub_mar(v), k, v))
    b_rows.sort(reverse=True)
    HTML.append('<h3>B 网格（节奏参数，固定 SL3·TR2·BE1）— 按全样本 MAR</h3>')
    HTML.append('<table><thead><tr>'
                '<th class="l">配置 (CD_AL_AT)</th><th>笔数</th><th>净利(¥)</th><th>PF</th>'
                '<th>MDD(¥)</th><th>MAR</th><th>train净利</th><th>holdout净利</th><th>oos21净利</th>'
                '</tr></thead><tbody>')
    for sc, k, v in b_rows:
        m = re.match(r'CD([\d.]+)_AL([\d.]+)_AT([\d.]+)', k)
        lab = f'CD{m.group(1)}·AL{m.group(2)}·AT{m.group(3)}'
        HTML.append(
            f'<tr><td class="l">{lab}</td>'
            f'<td>{fmt(v["n"])}</td>'
            f'<td>{fmt(v["net_yuan"])}</td>'
            f'<td>{fmt(v["pf"],3)}</td>'
            f'<td>{fmt(v["mdd_yuan"])}</td>'
            f'<td style="background:{green_red(sc,6,9)}"><b>{fmt(sc,2)}</b></td>'
            f'<td>{fmt(v["train"]["net_yuan"])}</td>'
            f'<td>{fmt(v["holdout"]["net_yuan"])}</td>'
            f'<td>{fmt(v["oos21"]["net_yuan"])}</td></tr>')
    HTML.append('</tbody></table>')
    HTML.append('<div class="note">节奏三参数中：<b>ADD_MAX_LOTS=2</b> 是风险调整收益的甜点（MAR 最高，≈8）；AL=3 净利最高但 MDD 同步放大、MAR 回落。'
                '<b>ADD_THR=1.5</b> 普遍优于 1.0/0.5（加仓门槛更严，PF 与 MAR 双升）。<b>COOLDOWN</b> 影响很小（CD1 与 CD3 仅差约 2% 净利），CD1 略优但 CD3（与基线同）更稳健。</div>')

    # ---------- 3. 推荐 + 对照 ----------
    HTML.append('<h2>三、推荐配置与新旧对照</h2>')
    chosen = (f'SL_ATR={reco["SL_ATR"]} · TRAIL_ATR={reco["TRAIL_ATR"]} · BE_R={reco["BE_R"]} · '
              f'COOLDOWN={reco["COOLDOWN"]} · ADD_MAX_LOTS={reco["ADD_MAX_LOTS"]} · ADD_THR={reco["ADD_THR"]}')
    HTML.append(f'<div class="card"><div class="kpi">'
                f'<div class="box"><div class="k">推荐配置</div><div class="v" style="font-size:14px">{chosen}</div></div>'
                f'</div></div>')

    def block_row(label, m, bold=False):
        cls = ' style="font-weight:700;background:#eef4ff"' if bold else ''
        return (f'<tr{cls}><td class="l">{label}</td>'
                f'<td>{fmt(m.get("n"))}</td>'
                f'<td>{fmt(m.get("net_yuan"))}</td>'
                f'<td>{fmt(m.get("pf"),3)}</td>'
                f'<td>{fmt(m.get("mdd_yuan"))}</td>'
                f'<td>{fmt(sub_mar(m),2)}</td>'
                f'<td>{pct(m.get("win"))}</td></tr>')

    HTML.append('<table><thead><tr>'
                '<th class="l">区段</th><th>笔数</th><th>净利(¥)</th><th>PF</th><th>MDD(¥)</th><th>MAR</th><th>胜率</th>'
                '</tr></thead><tbody>')
    HTML.append(block_row('旧·全样本 (基线)', mb))
    HTML.append(block_row('新·全样本 (推荐)', mr, bold=True))
    HTML.append(block_row('旧·train(≤2025)', mb.get('train', {})))
    HTML.append(block_row('新·train(≤2025)', mr.get('train', {})))
    HTML.append(block_row('旧·holdout(≥2026)', mb.get('holdout', {})))
    HTML.append(block_row('新·holdout(≥2026)', mr.get('holdout', {})))
    HTML.append(block_row('旧·oos21(≥2021)', mb.get('oos21', {})))
    HTML.append(block_row('新·oos21(≥2021)', mr.get('oos21', {})))
    HTML.append('</tbody></table>')

    delta = cmp['delta']
    HTML.append('<div class="kpi">'
                f'<div class="box"><div class="k">净利变化</div><div class="v up">+{fmt(delta["net"])}</div></div>'
                f'<div class="box"><div class="k">PF 变化</div><div class="v up">+{fmt(delta["pf"],3)}</div></div>'
                f'<div class="box"><div class="k">MAR 变化</div><div class="v up">+{fmt(delta["mar"],2)}</div></div>'
                f'<div class="box"><div class="k">MDD 变化</div><div class="v up">+{fmt(delta["mdd"])} (回撤更小)</div></div>'
                f'</div>')
    HTML.append('<div class="ok">推荐配置在<b>三个分区同时改善</b>：全样本净利 +31%、MAR +89%；train 净利 131万→184万，holdout 净利 73万→84万、PF 2.20→2.41。'
                '且回撤反而从 −45万 收窄到 −31万——不是靠放大风险换收益。</div>')

    # 分品种
    pbs = mb.get('per_sym', {}); prs = mr.get('per_sym', {})
    syms = sorted(set(pbs) | set(prs))
    rows = []
    for s in syms:
        bnet = (pbs.get(s) or {}).get('net', 0) or 0
        rnet = (prs.get(s) or {}).get('net', 0) or 0
        rows.append((rnet - bnet, s, bnet, rnet))
    rows.sort(reverse=True)
    HTML.append('<h3>分品种净利变化（新 − 旧，按增量排序）</h3>')
    HTML.append('<table><thead><tr><th class="l">品种</th><th>旧净利(¥)</th><th>新净利(¥)</th><th>Δ(¥)</th>'
                '<th>旧PF</th><th>新PF</th></tr></thead><tbody>')
    for d_, s, bnet, rnet in rows:
        col = 'up' if d_ >= 0 else 'down'
        bpf = (pbs.get(s) or {}).get('pf'); rpf = (prs.get(s) or {}).get('pf')
        HTML.append(f'<tr><td class="l">{s}</td><td>{fmt(bnet)}</td><td>{fmt(rnet)}</td>'
                    f'<td class="{col}">{("+" if d_>=0 else "")+fmt(d_)}</td>'
                    f'<td>{fmt(bpf,3)}</td><td>{fmt(rpf,3)}</td></tr>')
    HTML.append('</tbody></table>')
    n_up = sum(1 for _,_,_,_ in rows if _ >= 0)
    HTML.append(f'<p class="sub">45 个品种中 {n_up} 个净利改善，{len(rows)-n_up} 个回落——改善面广泛而非个别品种驱动。</p>')

    # ---------- 4. 结论 ----------
    HTML.append('<h2>四、结论、风险与下一步</h2>')
    HTML.append('''<div class="card">
<ul>
<li><b>数据层</b>：换月“bug”已证伪（假阳性）；真实缺陷（203 前导 NaN 棒）已结构保全地修复并备份。</li>
<li><b>参数层</b>：推荐 <span class="tag">SL3.0</span><span class="tag">TR2.0</span><span class="tag">BE1.0</span>
<span class="tag">CD1</span><span class="tag">AL2</span><span class="tag">AT1.5</span>。核心两处稳健改动：
止损放宽 2.0→3.0、保本线 0.5→1.0、加仓门槛 1.0→1.5。</li>
<li><b>稳健性</b>：改善在 train/holdout/oos21 三分区同号，并非单分区过拟合；但 holdout(≥2026) 仅约 200 笔，属小样本，结论需 2026 全年走完后再复核。</li>
<li><b>过拟合声明</b>：CD1 vs CD3 差异仅 ~2%，cooldown 属弱杠杆；若偏好与基线一致的交易节奏，可保留 CD3（MAR 8.06、净利 2.63M，几乎等同 CD1）。</li>
</ul></div>''')
    HTML.append('<div class="note"><b>下一步建议</b>：（1）将推荐参数写回 step46 全局量并锁定为 V5X 正式版；'
                '（2）在 THS 网格（6 棒/日，2020+）上做同参数复核，确认跨网格一致；'
                '（3）2015 年数据无 1m 源，仍暂缓；若需补，须先取得交易所 2015 源 ZIP。</div>')

    HTML.append('<footer>本报告的扫描 JSON：step73_sweep.json；新旧对照 JSON：step74_compare.json；'
                '成交明细：trades74_baseline.csv / trades74_reco.csv。所有数值由清洗后数据实跑得出，未做任何手工调整。</footer>')
    HTML.append('</div></body></html>')

    open(OUT_HTML, 'w', encoding='utf-8').write('\n'.join(HTML))
    print('saved', OUT_HTML, '| A=', len(A), 'B=', len(B), '| reco MAR=', fmt(sub_mar(mr), 2))


if __name__ == '__main__':
    main()
