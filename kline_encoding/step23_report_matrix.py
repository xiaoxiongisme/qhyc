# -*- coding: utf-8 -*-
"""生成 matrix_analysis_report.html：矩阵/数组/线性代数视角汇总报告"""
import json, numpy as np

OUT = '.'
M = json.load(open(f'{OUT}/results_matrix.json', encoding='utf-8'))
L = json.load(open(f'{OUT}/results_linalg.json', encoding='utf-8'))
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
INK, SUB, FAINT = '#26215C', '#5F5E5A', '#888780'
RED, GREEN, PUR = '#C4372E', '#2E7D4F', '#5C4FA1'
RAMP = ['#F6F4EE', '#EAE6F4', '#CFC6E6', '#A397D2', '#6E5FB8', '#3E3480']


def svg(name):
    return open(f'_w_{name}.svg', encoding='utf-8').read()


def th(cells):
    return '<tr>' + ''.join(f'<th>{c}</th>' for c in cells) + '</tr>'


def td(cells, cls=None):
    c = f' class="{cls}"' if cls else ''
    return f'<tr{c}>' + ''.join(f'<td>{x}</td>' for x in cells) + '</tr>'


def table(headers, rows):
    return f'<table><thead>{th(headers)}</thead><tbody>{"".join(rows)}</tbody></table>'


def pct(x, d=1):
    return f'{x * 100:.{d}f}%'


CSS = """
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#FBFAF7;color:#26215C;margin:0;padding:32px 40px;line-height:1.65}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:24px;font-weight:700;margin:0 0 4px}
h2{font-size:18px;font-weight:700;margin:36px 0 10px;padding-left:10px;border-left:4px solid #5C4FA1}
h3{font-size:14.5px;font-weight:700;margin:22px 0 8px}
.sub{color:#888780;font-size:12.5px;margin-bottom:18px}
.box{background:#fff;border:1px solid #E5E2D8;border-radius:10px;padding:14px 16px;margin:12px 0;font-size:13px}
.box.ok{border-left:4px solid #2E7D4F;background:#F4FAF6}
.box.warn{border-left:4px solid #EF9F27;background:#FDF9F0}
.box.bad{border-left:4px solid #C4372E;background:#FBF4F3}
table{border-collapse:collapse;width:100%;font-size:12px;margin:10px 0;background:#fff}
th{background:#F6F4EE;color:#26215C;font-weight:600;padding:6px 8px;border:1px solid #E5E2D8;text-align:center}
td{padding:5px 8px;border:1px solid #EFEDE4;text-align:center}
td.l{text-align:left}
tr:nth-child(even) td{background:#FCFBF8}
.fig{background:#fff;border:1px solid #E5E2D8;border-radius:10px;padding:10px;margin:14px 0}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.p-pass{background:#E4F2E9;color:#2E7D4F}.p-fail{background:#F9E7E5;color:#C4372E}.p-mid{background:#FBF0DC;color:#9A6209}
code{background:#F1EFE7;border-radius:4px;padding:1px 5px;font-size:11.5px}
.mut{color:#888780;font-size:11.5px}
b.r{color:#C4372E}b.g{color:#2E7D4F}b.p{color:#5C4FA1}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.grid2{grid-template-columns:1fr}}
"""

H = ['<div class="wrap">']
H.append('<h1>矩阵 / 数组 / 线性代数视角：六位编码数据还能读出什么</h1>')
H.append('<div class="sub">甲醇 MA + 乙二醇 EG · 主力连续 1 分钟 · 2020-2025 全样本 489,975 根/品种 · 会话内 60 秒相邻对 484,255 个 · 2026-10-17</div>')

# ============ 一、总览 ============
H.append('<h2>一、九种矩阵视角总览</h2>')
rows = [
    td(['<b>① 转移矩阵</b> 8×8', 'P(下一形态|当前形态)', '状态动力学：平稳分布、驻留、谱间隙', '形状无记忆（混合≈1根），可预测的只有幅度'], cls='l'),
    td(['<b>② 转移张量</b> 年×8×8', '6 个年度矩阵', '跨年漂移（Frobenius 距离）', '0.08~0.35，regime 相关，须滚动估计'], cls='l'),
    td(['<b>③ 互信息矩阵</b> 6×6', '字段两两 MI', '信息冗余地图', '实体↔涨跌幅 1.14 bit；CLV 是枢纽'], cls='l'),
    td(['<b>④ 日×时段矩阵</b> 1715×345', 'SVD 分解', '波动率可分离低秩结构', 'σ₁ 独占 72.5%，有效秩 5.4'], cls='l'),
    td(['<b>⑤ 日×形态占用矩阵</b>', 'SVD 因子', '日级 regime 因子', '因子1~次日振幅 0.70，~次日收益 −0.015'], cls='l'),
    td(['<b>⑥ 距离矩阵</b> 8×8', '特征空间欧氏距离', '八态几何结构', '聚类=2家族×4影线；错卦对最远'], cls='l'),
    td(['<b>⑦ 协方差矩阵</b> Σ 5×5', '特征分解', '有效维度、共线性', '参与率 3.68；振幅 VIF=1.0 唯一正交'], cls='l'),
    td(['<b>⑧ 滞后协方差</b> Γ₁ 5×5', '典型相关分析', '线性可预测性上限', '平均 R²=0.091，全在振幅方向'], cls='l'),
    td(['<b>⑨ 格拉姆矩阵</b> 8×8', '状态质心内积', '状态云的有效秩', '2.65——八态质心只张成 ~2.7 维'], cls='l'),
]
H.append(table(['视角', '矩阵', '读出的信息', '核心结论'], rows))

# ============ 二、转移矩阵 ============
H.append('<h2>二、转移矩阵与谱分析：形状是“无记忆”的</h2>')
H.append(f'<div class="fig">{svg("transfer")}</div>')
t = M['MA']['trans']
rows = []
for i, s in enumerate(STATES):
    rows.append(td([f'{TRIG[s]} <code>{s}</code>', pct(t['pi_theory'][i]), pct(t['pi_emp'][i]),
                    pct(t['self_trans'][i], 0), f'{t["sojourn_obs"][i]:.1f}', f'{t["row_entropy"][i]:.2f}'], cls='l'))
H.append('<h3>谱分析量（MA，EG 见 JSON）</h3>')
H.append(table(['形态', '平稳分布(理论)', '平稳分布(经验)', '自转移概率', '驻留(实测,根)', '行熵(bit)'], rows))
te = M['EG']['trans']
H.append(f'''<div class="box ok"><b>马尔可夫假设几乎精确成立，但这恰恰是最重要的负面信息。</b>
理论平稳分布与经验分布最大只差 <b>{t['pi_maxdiff'] * 100:.2f}</b>（MA）/<b>{te['pi_maxdiff'] * 100:.2f}</b>（EG）个百分点，驻留时间的理论值（几何分布 1/(1−pᵢᵢ)）与实测逐一吻合——
八态转移矩阵是一个合格的线性算子。<b class="r">但它的谱间隙高达 {t['spectral_gap']:.3f}，混合时间约 {t['mixing_bars']:.1f} 根</b>：
当前形态对下一根形态几乎没有影响力（自转移仅 2%~26%）。矩阵诚实地告诉你：<b>“下一根长什么样”在 1 分钟尺度基本不可预测</b>，
而 V4/V6 报告里真正可预测的“下一根多大”不在这张 8×8 表里——因为原编码把幅度维丢掉了（见第八节）。</div>''')
H.append(f'''<div class="box"><b>第二特征向量读出唯一慢模态。</b>P 的特征值 |λ|=[1, {t['eig_abs'][1]:.2f}, {t['eig_abs'][2]:.2f}, …]，
第二特征向量（MA）在形态序列 [离艮震坤|乾巽兑坎] 上为 [-1.0,-0.4,+0.4,+0.9|-0.3,-0.8,+0.4,+0.8]——
正号一侧全是<b>有上影</b>态（震兑坎坤），负号一侧全是<b>无上影</b>态（离艮乾巽）。即转移动力学的主导慢模态是
<b>“上影/无上影”的交替</b>（λ₂={t['eig_abs'][1]:.2f}，半衰期仅 0.3 根）。这与 V3 报告“000100（上影阴→涨）是最强跨周期信号”互相印证：
矩阵的慢模态和信号的强模态是同一件事。</div>''')
rows = [td([str(y), pct(M['MA']['trans_tensor']['Fro_rel'][str(y)]), pct(M['EG']['trans_tensor']['Fro_rel'][str(y)])], cls='l') for y in M['MA']['trans_tensor']['Fro_rel']]
H.append('<h3>跨年转移矩阵漂移（相对合并矩阵的 Frobenius 距离）</h3>')
H.append(table(['年份', 'MA', 'EG'], rows))
H.append('<div class="mut">2021/2022 漂移最大（单边趋势年），2020/2023 最小——转移矩阵本身是 regime 相关的，实盘应按滚动窗口估计而非固定。</div>')

# ============ 三、互信息矩阵 ============
H.append('<h2>三、互信息矩阵：字段冗余地图</h2>')
H.append(f'<div class="fig">{svg("mi")}</div>')
mi = M['MA']['mi']; mie = M['EG']['mi']
keys = mi['keys']
Mm = np.array(mi['matrix']); Me = np.array(mie['matrix'])
hdr = [''] + keys
rows = []
for i in range(6):
    rows.append(td([f'<b>{keys[i]}</b>'] + [f'{Mm[i, j]:.2f}' + (f'<span class="mut"> / {Me[i, j]:.2f}</span>' if i != j else '') for j in range(6)], cls='l'))
H.append('<h3>6×6 互信息矩阵（比特，MA / EG）</h3>')
H.append(table(hdr, rows))
rows = []
for k, v in mi['pred'].items():
    if v is None:
        continue
    ve = mie['pred'][k]
    rows.append(td([k, f"{v['dir']:.4f}", f"{ve['dir']:.4f}", f"{v['amp']:.4f}", f"{ve['amp']:.4f}"], cls='l'))
H.append(table(['字段（当前根）', 'MI→下一根涨跌 MA', 'EG', 'MI→下一根振幅 MA', 'EG'], rows))
H.append(f'''<div class="box warn"><b>矩阵一眼定位冗余与缺口。</b>
<b>实体三态 × 涨跌幅 MI = 1.14 / 1.30 bit</b>——实体字段几乎就是涨跌幅的符号量化，二者同时入模是纯冗余（VIF 也证实：涨跌幅 VIF=2.9）；
CLV 是第二枢纽（与涨跌幅 0.59/0.62、与实体 0.55/0.53）。<b>真正的缺口在预测端</b>：
“当前振幅 5 档”对下一根振幅的 MI 达 <b>0.154 / 0.241 bit</b>，而<b>整个六位形态码只有 0.028 / 0.037</b>——
编码把预测力最强的连续量（振幅）压成了 3 个二值位，信息在量化时大量漏损。这是“用矩阵视角独立复现 V6 优化结论”。</div>''')

# ============ 四、日×时段矩阵 ============
H.append('<h2>四、日 × 时段矩阵：波动率是可分离的低秩结构</h2>')
H.append(f'<div class="fig">{svg("daymat")}</div>')
dm, dme = M['MA']['daymat'], M['EG']['daymat']
rows = [
    td(['矩阵规模', f"{dm['n_days']} × 345", f"{dme['n_days']} × 345"], cls='l'),
    td(['σ₁ 能量占比', pct(dm['evr_top'][0]), pct(dme['evr_top'][0])], cls='l'),
    td(['σ₁+σ₂', pct(dm['evr_top'][0] + dm['evr_top'][1]), pct(dme['evr_top'][0] + dme['evr_top'][1])], cls='l'),
    td(['有效秩', f"{dm['eff_rank']:.2f}", f"{dme['eff_rank']:.2f}"], cls='l'),
    td(['日载荷 ~ 当日平均振幅', f"{dm['load_corr']['amp']:.3f}", f"{dme['load_corr']['amp']:.3f}"], cls='l'),
    td(['日载荷 ~ 次日平均振幅', f"{dm['load_corr']['next_amp']:.3f}", f"{dme['load_corr']['next_amp']:.3f}"], cls='l'),
    td(['日载荷 ~ 当日收益', f"{dm['load_corr']['ret']:+.3f}", f"{dme['load_corr']['ret']:+.3f}"], cls='l'),
    td(['占用矩阵因子1 ~ 次日收益', f"{dm['f1_next_ret']:+.3f}", f"{dme['f1_next_ret']:+.3f}"], cls='l'),
    td(['占用矩阵因子1 ~ 次日振幅', f"{dm['f1_next_amp']:.3f}", f"{dme['f1_next_amp']:.3f}"], cls='l'),
]
H.append(table(['统计量', 'MA', 'EG'], rows))
H.append(f'''<div class="box ok"><b>rank-1 主导 = “日内轮廓 × 日波动水平”的可分离结构。</b>
第一右奇异向量（轮廓1，占 {dm['evr_top'][0] * 100:.1f}% 能量）就是标准日内波动曲线：开盘尖峰→上午中段回落→午休平静→午后回升→尾盘翘起。
逐 slot 直接平均验证：开盘段平均振幅 <b>0.139%</b>，午休/下午/夜盘三段基本持平（0.118%~0.120%）——开盘段高出约 17%。
日载荷与当日平均振幅相关 <b>{dm['load_corr']['amp']:.2f}</b>、与次日振幅 <b>{dm['load_corr']['next_amp']:.2f}</b>（波动聚集的日级版本），
但与当日收益只有 <b>{dm['load_corr']['ret']:+.3f}</b>。<b>高波动日不预测方向，只预测继续高波动</b>——
这条矩阵结论是“仓位/止损标定”的直接依据：用 σ₁ 日载荷做仓位缩放，用不到方向信息。
<span class="mut">口径说明：1715 个交易日中有 260 行（15.2%）只有夜盘（节后首个交易日），其日盘槽位以 0 填充；
SVD 轮廓与逐 slot nanmean 轮廓相关 0.936，上述定性结论不受影响。</span></div>''')

# ============ 五、距离矩阵 ============
H.append('<h2>五、状态距离矩阵：八态的真实几何</h2>')
H.append(f'<div class="fig">{svg("dist")}</div>')
dd, de = M['MA']['dist'], M['EG']['dist']
rows = [
    td(['全体平均距离', f"{dd['d_mean']:.2f}", f"{de['d_mean']:.2f}"], cls='l'),
    td(['“其他对”平均（差 1 个字段）', f"{dd['d_other']:.2f}", f"{de['d_other']:.2f}"], cls='l'),
    td(['<b>综卦对</b>（影线换边：震↔艮、巽↔兑）', f"<b class='p'>{dd['d_zong']:.2f}</b>", f"<b class='p'>{de['d_zong']:.2f}</b>"], cls='l'),
    td(['<b>错卦对</b>（三爻全反：乾↔坤 等 4 对）', f"<b class='r'>{dd['d_cuo']:.2f}</b>", f"<b class='r'>{de['d_cuo']:.2f}</b>"], cls='l'),
]
H.append(table(['距离统计', 'MA', 'EG'], rows))
order = dd['order']
H.append('<div class="mut">聚类顺序（MA）：' + ' → '.join(TRIG[STATES[i]] for i in order) + '</div>')
H.append(f'''<div class="box bad"><b>诚实的负面结果：易理关系不映射到数据几何。</b>
按特征（平均涨跌/振幅/CLV/上影/下影）做的层次聚类，顺序干净地分成<b>阳线四态（乾巽兑坎）与阴线四态（离震艮坤）两大家族</b>，家族内按影线模式（无/下/上/双）排列——
真实结构是“实体方向 × 影线模式”，不是八卦的错/综关系。<b>错卦对确实最远</b>（{dd['d_cuo']:.2f} vs 平均 {dd['d_mean']:.2f}，完全反向的K线差异最大，符合直觉），
但<b>“综卦对”比平均距离还远</b>（{dd['d_zong']:.2f} vs {dd['d_other']:.2f}）——“影线换边”不是小改动：它让 CLV 从 0 翻到 1，是跨半场的移动。
结论：八卦映射作为<b>命名法</b>好用，作为<b>几何结构</b>不成立。</div>''')

# ============ 六、PCA ============
H.append('<h2>六、PCA：一根K线在特征空间里是 3 维物体</h2>')
H.append(f'<div class="fig">{svg("pca")}</div>')
pc, pce = M['MA']['pca'], M['EG']['pca']
feats = ['涨跌幅', '振幅', 'CLV', '上影', '下影']
rows = [td([feats[i]] + [f'{pc["loadings"][k][i]:+.2f}' for k in range(3)] + [f'{pce["loadings"][k][i]:+.2f}' for k in range(3)], cls='l') for i in range(5)]
H.append(table(['特征', 'PC1 MA', 'PC2 MA', 'PC3 MA', 'PC1 EG', 'PC2 EG', 'PC3 EG'], rows))
H.append(f'''<div class="box ok"><b>三个正交主轴，恰好对应三件事：位置、方向、大小。</b>
PC1（{pc['evr'][0] * 100:.1f}% 方差）= <b>收盘位置轴</b>（CLV −0.68、上影 +0.42、下影 −0.42）；PC2（{pc['evr'][1] * 100:.1f}%）= <b>方向轴</b>（涨跌幅 +0.73）；PC3（{pc['evr'][2] * 100:.1f}%）= <b>大小轴</b>（振幅独占 ±0.93）。
三轴合计 <b>{pc['cum3'] * 100:.0f}%</b> 方差。<b class="r">原六位码 = PC2 的 1 比特 + PC1 的 2 比特 + PC3 的 0 比特</b>——
它在矩阵空间里的定位被 PCA 一眼看穿：丢掉的正是第三主轴。这就是“更小维度、更多信息”的线性代数答案：存 (方向, CLV, 振幅) 三个数，或直接存 PC1/PC2/PC3 三个得分。</div>''')

# ============ 七、交叉矩阵 ============
H.append('<h2>七、甲醇 × 乙二醇：交叉相关与联合占用矩阵</h2>')
H.append(f'<div class="fig">{svg("cross")}</div>')
c = M['cross']
rows = []
for k in range(-10, 11):
    rows.append(td([f'{k:+d}', f"{c['lead']['chg'][str(k)]:+.3f}", f"{c['lead']['amp'][str(k)]:+.3f}"], cls='l'))
H.append('<div class="grid2"><div><h3>领先滞后互相关（k=甲醇领先分钟数）</h3>' + table(['k', '涨跌幅', '振幅'], rows) + '</div>')
J = np.array(c['joint'], dtype=float)
J = J / J.sum() * 100
hdr = ['MA\\EG'] + [TRIG[s] for s in STATES]
rows = [td([TRIG[STATES[i]]] + [f'{J[i, j]:.1f}' for j in range(8)], cls='l') for i in range(8)]
H.append('<div><h3>联合占用矩阵 %</h3>' + table(hdr, rows) + '</div></div>')
H.append(f'''<div class="box"><b>两个矩阵都指向“同步共动、无领先”。</b>
领先滞后曲线峰值在 k=0（同刻涨跌幅相关 {c['corr_chg']:.3f}、振幅相关 {c['corr_amp']:.3f}），向两侧迅速衰减；
联合占用矩阵的互信息 <b>{c['joint_MI']:.3f} 比特</b>（置换零假设 {c['joint_MI_null']:.4f}，p={c['joint_MI_p']:.3f}，边际显著），
最高共现格是 <b>坎☵-坎☵ {J[7, 7]:.1f}%</b>——两个品种在同时刻一起进入“双影阳线”高波动态。
工程用途：把联合矩阵当<b>风控共振探测器</b>（同一分钟两品种同态=系统性波动放大），不要当领先信号（k=0 已否定）。</div>''')

# ============ 八、线性代数专题 ============
H.append('<h2>八、线性代数专题：Σ、Γ₁、格拉姆矩阵、投影回归</h2>')
H.append(f'<div class="fig">{svg("sigma")}</div>')
H.append(f'<div class="fig">{svg("pred")}</div>')
la, lae = L['MA'], L['EG']
rows = [
    td(['特征值 λ₁..λ₅', ' / '.join(f'{x:.2f}' for x in la['Sigma']['eig']), ' / '.join(f'{x:.2f}' for x in lae['Sigma']['eig'])], cls='l'),
    td(['条件数', f"{la['Sigma']['cond']:.1f}", f"{lae['Sigma']['cond']:.1f}"], cls='l'),
    td(['谱熵（上限 2.32 bit）', f"{la['Sigma']['spec_entropy_bits']:.2f}", f"{lae['Sigma']['spec_entropy_bits']:.2f}"], cls='l'),
    td(['参与率（有效维度）', f"{la['Sigma']['participation_ratio']:.2f}", f"{lae['Sigma']['participation_ratio']:.2f}"], cls='l'),
    td(['平方典型相关（前 3 个方向）', ' / '.join(f'{x:.3f}' for x in la['Gamma1']['canonical_r2'][:3]), ' / '.join(f'{x:.3f}' for x in lae['Gamma1']['canonical_r2'][:3])], cls='l'),
    td(['平均线性可预测方差', f"{la['Gamma1']['avg_r2']:.4f}", f"{lae['Gamma1']['avg_r2']:.4f}"], cls='l'),
    td(['逐坐标可预测 R²（涨跌/振幅/CLV/上影/下影）', ' / '.join(f'{x:.3f}' for x in la['Gamma1']['r2_coord']), ' / '.join(f'{x:.3f}' for x in lae['Gamma1']['r2_coord'])], cls='l'),
    td(['每 PC 可预测性', ' / '.join(f'{x:.3f}' for x in la['Gamma1']['pred_per_pc']), ' / '.join(f'{x:.3f}' for x in lae['Gamma1']['pred_per_pc'])], cls='l'),
    td(['范数自相关 acf1(‖z‖)', f"{la['norm']['acf1_norm']:.3f}", f"{lae['norm']['acf1_norm']:.3f}"], cls='l'),
    td(['acf1(振幅坐标)', f"{la['norm']['acf1_amp_z']:.3f}", f"{lae['norm']['acf1_amp_z']:.3f}"], cls='l'),
    td(['六位码捕获率（5 维均值）', pct(la['capture']['r2_code8_mean']), pct(lae['capture']['r2_code8_mean'])], cls='l'),
    td(['PCA-3 捕获率', pct(la['capture']['r2_pca3']), pct(lae['capture']['r2_pca3'])], cls='l'),
    td(['格拉姆矩阵有效秩（8 态质心）', f"{la['gram']['rank_eff']:.2f}", f"{lae['gram']['rank_eff']:.2f}"], cls='l'),
    td(['距离恒等式校验误差', f"{la['gram']['identity_max_err']:.1e}", f"{lae['gram']['identity_max_err']:.1e}"], cls='l'),
    td(['投影回归 R²（前 5 特征）', f"{la['projection']['R2_prev5']:.4f}", f"{lae['projection']['R2_prev5']:.4f}"], cls='l'),
    td(['＋8 态哑变量后 R²', f"{la['projection']['R2_prev5_plus_dummies']:.4f}", f"{lae['projection']['R2_prev5_plus_dummies']:.4f}"], cls='l'),
    td(['残差范数 / 中心化 y 范数', f"{la['projection']['resid_norm_ratio']:.3f}", f"{lae['projection']['resid_norm_ratio']:.3f}"], cls='l'),
]
H.append(table(['线性代数量', 'MA', 'EG'], rows))
vifr = td([f'<b>{f}</b>', f"{la['vif'][i]:.1f}", f"{lae['vif'][i]:.1f}"] for i, f in enumerate(['涨跌幅', '振幅', 'CLV', '上影', '下影']))
H.append('<h3>方差膨胀因子 VIF（共线性诊断）</h3>' + table(['特征', 'MA', 'EG'], vifr))
H.append(f'''<div class="box ok"><b>线性代数把“为什么预测不动方向”变成了一个投影问题。</b>
把每根K线当作 5 维标准化向量 z，滞后协方差 Γ₁=Cov(z_t, z_(t+1)) 的典型相关分析给出线性可预测性的硬上限：
<b>平均可预测方差仅 {la['Gamma1']['avg_r2'] * 100:.1f}%</b>（MA）/<b>{lae['Gamma1']['avg_r2'] * 100:.1f}%</b>（EG），
且几乎全部集中在<b>振幅坐标</b>（逐坐标 R²：振幅 {la['Gamma1']['r2_coord'][1] * 100:.1f}% vs 涨跌 {la['Gamma1']['r2_coord'][0] * 100:.1f}% vs CLV {la['Gamma1']['r2_coord'][2] * 100:.1f}%）。
几何表述：把 y（下一根涨跌）向 X（当前 5 特征）的列空间做正交投影，投影长度只占 y 中心化长度的 <b>{(1 - la['projection']['resid_norm_ratio']) * 100:.1f}%</b>——
<b>残差向量仍是原向量的 {la['projection']['resid_norm_ratio']:.1%}，预测值是垂直于残差的一根细针</b>。残差正交性校验 5.7e-15（机器精度）证明最小二乘解的正确性。</div>''')
H.append(f'''<div class="box warn"><b>VIF 指出编码丢掉了全场唯一正交的坐标。</b>五个特征的方差膨胀因子：振幅 <b>1.0</b>（与任何其他坐标都不共线）、
上影/下影 1.7、涨跌幅 2.9、CLV 4.4。<b>六位码把 5 维压成 3 个二值位后，振幅维只剩 11.9% 捕获率</b>（CLV/影线维 74~84%）——
丢的恰好是最独立、最可预测（PC3 可预测性 42.7% 全场最高）的那一维。格拉姆矩阵则显示 8 个状态质心只张成 <b>{la['gram']['rank_eff']:.2f}</b> 维：
八态标签虽然多，几何上接近一个 2.7 维的云——“实体方向 × 影线模式”两个因子就几乎说完了。</div>''')

# ============ 九、年占用矩阵 ============
H.append('<h2>九、年 × 形态占用矩阵：市场的“年度性格”</h2>')
yo = M['MA']['year_occ']; yoe = M['EG']['year_occ']
hdr = ['年份'] + [TRIG[s] for s in STATES]
rows = []
for k, y in enumerate(yo['years']):
    rows.append(td([f'<b>{y}</b> MA'] + [pct(yo['occ'][k][j], 1) for j in range(8)], cls='l'))
    rows.append(td([f'<b>{y}</b> EG'] + [pct(yoe['occ'][k][j], 1) for j in range(8)], cls='l'))
H.append(table(hdr, rows))
H.append('''<div class="box"><b>占用矩阵按年切片就是一张 regime 表。</b>2021-2022 双影阴（坤☷）占到 13%~23%（单边高波动年），
2024-2025 平盘态升到 26%（平静年）。矩阵不需要任何模型就能读出“今年的市场性格”，可以直接作为策略开关的粗粒度输入：
连续 N 天平盘态占比 &gt; 20% → 降低日内频率假设；坤态占比 &gt; 15% → 上调止损宽度。</div>''')

# ============ 十、结论 ============
H.append('<h2>十、结论：矩阵视角的五条工程结论</h2>')
H.append('''<div class="box ok">
<b>1. 转移矩阵说“形状无记忆”</b>：谱间隙 0.87、驻留 1.0~1.3 根、平稳分布与经验分布只差 0.17pp——八态动力学是一个合格的马尔可夫链，但混合太快，形态本身不能当状态机用。<br>
<b>2. 滞后协方差说“可预测性全在振幅方向”</b>：平均线性可预测方差 9.1%，逐坐标 R² 振幅 23.6% / 涨跌 2.2% / CLV 0.1%；范数自相关 0.205 vs 振幅坐标 0.491——波动率聚集住在单个坐标里，不在整个向量里。<br>
<b>3. 协方差矩阵说“有效维度 3.7，正交坐标只有振幅”</b>：参与率 3.68、VIF 振幅=1.0；六位码在振幅维只捕获 11.9%，在 CLV/影线维捕获 74~84%。<br>
<b>4. PCA 说“一根K线是 3 维物体”</b>：PC1 位置（39%）、PC2 方向（23%）、PC3 大小（21%），三轴 83%；六位码 = PC2 的 1bit + PC1 的 2bit + PC3 的 0bit。<br>
<b>5. 日×时段矩阵说“波动率可分离、低秩”</b>：σ₁ 独占 72.5%、有效秩 5.4；日载荷预测次日振幅（0.64）不预测方向（−0.067）——仓位标定的矩阵基础。<br>
<b>6. 距离矩阵给八卦映射一个诚实判决</b>：聚类结构 = 实体方向 × 影线模式；错卦对最远（符合直觉），综卦对并不近（CLV 翻转）——命名法好用，几何学不成立。
</div>''')
H.append('<div class="mut">数据：MA/EG 2020-2025 主力连续 1 分钟（CZCE/DCE），489,975 根/品种；相邻对定义为会话内 gap=60s（484,255 对）。'
         '脚本 step20_matrix.py / step22_linalg.py，结果 results_matrix.json / results_linalg.json。EG 全部结论同向，量级见表内双列。</div>')
H.append('</div>')

html = f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>矩阵视角分析</title><style>{CSS}</style></head><body>{''.join(H)}</body></html>'
open(f'{OUT}/matrix_analysis_report.html', 'w', encoding='utf-8').write(html)
print('report chars:', len(html), '| svg:', html.count('<svg'), '| tables:', html.count('<table'))
