# -*- coding: utf-8 -*-
"""生成多维度分析报告 dimensions_report.html"""
import json

R = json.load(open('results_dimensions.json', encoding='utf-8'))
MA, EG, TAIL = R['MA'], R['EG'], R['tail']
STATES = ['000000', '000001', '000100', '000101', '010000', '010001', '010100', '010101']
TRIG = {'000000': '离☲', '000001': '艮☶', '000100': '震☳', '000101': '坤☷',
        '010000': '乾☰', '010001': '巽☴', '010100': '兑☱', '010101': '坎☵'}
FIGS = {k: open(f'_d_{k}.svg', encoding='utf-8').read() for k in
        ['entropy', 'order', 'graph', 'freq', 'stability', 'scaling', 'cond', 'tail']}

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
@media(max-width:800px){.grid2{display:block}}
"""


def fig(k):
    return f'<div class="fig">{FIGS[k]}</div>'


def tbl(headers, rows):
    h = ''.join(f'<th>{x}</th>' for x in headers)
    b = ''.join('<tr>' + ''.join(f'<td class="l">{c}</td>' if i == 0 and len(str(c)) > 6 else f'<td>{c}</td>'
               for i, c in enumerate(r)) + '</tr>' for r in rows)
    return f'<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'


e, ee = MA['entropy'], EG['entropy']
m, mm = MA['memory'], EG['memory']
g = MA['graph']
f, ff = MA['freq'], EG['freq']
st, se = MA['stability'], EG['stability']
sc, sce = MA['scaling'], EG['scaling']
c, ce = MA['cond'], EG['cond']

H = [f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
     f'<meta name="viewport" content="width=device-width,initial-scale=1">'
     f'<title>多维度分析报告</title><style>{CSS}</style></head><body><div class="wrap">']
H.append('<h1>再多点维度：矩阵与线性代数之外的八个分析视角</h1>')
H.append('<div class="sub">甲醇 MA + 乙二醇 EG · 主力连续 1 分钟 · 2020-2025 训练 489,975 根/品种 + 2026 holdout 35,865 根/品种 · '
         '口径：夜盘归属次日交易日、会话内 60 秒相邻对 · 2026-10-17</div>')

# ---------- 总览 ----------
H.append('<h2>一、八个新维度总览</h2>')
H.append(tbl(['维度', '数学对象', '关键读数（MA / EG）', '判决'],
             [['① 信息论与压缩', '熵、块熵、冗余度',
               f'熵率 {e["block"]["3"]:.3f} / {ee["block"]["3"]:.3f} bit，压缩率 {e["compress_ratio"]*100:.1f}% / {ee["compress_ratio"]*100:.1f}%',
               '<span class="pill p-pass">六位码过半是冗余</span>'],
              ['② 记忆深度', 'k 阶马尔可夫链 holdout 损失',
               f'增益 +{m["gain_1_over_0"]:.3f} / +{mm["gain_1_over_0"]:.3f}（一阶），+{m["gain_2_over_1"]:.3f} / +{mm["gain_2_over_1"]:.3f}（二阶）',
               '<span class="pill p-mid">二阶仍有增量，三阶饱和</span>'],
              ['③ 图论', '加权有向图 / 社区 / PageRank',
               f'显著边 {g["n_sig_edges"]}/64，社区=[阴4态|阳4态]，Q={g["modularity"]:.3f}',
               '<span class="pill p-pass">拓扑复现阴阳两族</span>'],
              ['④ 频域与节律', 'FFT / 星期×时段矩阵 / 月度',
               f'周一振幅为周二~周五的 {f["dow_mean"][0]/sum(f["dow_mean"][1:])*4:.2f} 倍；月峰谷比 1.39',
               '<span class="pill p-pass">周一效应 + 三月高峰</span>'],
              ['⑤ 结构稳定性', '跨年 Fro 漂移 / χ² / CUSUM',
               f'漂移 0.08–0.33；2026 vs 训练 {st["Fro_2026_vs_train"]:.2f}；CUSUM 无变点',
               '<span class="pill p-mid">统计显著、量级小、无退化</span>'],
              ['⑥ 长程标度', 'DFA / 方差比 / 池化 ACF',
               f'DFA α₁ₘ={sc["dfa_1m_alpha"]:.2f}；VR(10)={sc["var_ratio"]["10"]:.2f}；ACF₆₀={list(sc["amp_acf"].values())[-1]:.2f}',
               '<span class="pill p-pass">波动长记忆、收益短程回归</span>'],
              ['⑦ 条件结构', 'P(下一态|当前态) / JS 距离',
               f'综卦对 JS={c["js_zong_mean"]:.4f} vs 错卦对 {c["js_cuo_mean"]:.4f}',
               '<span class="pill p-fail">推翻「综卦=大变动」</span>'],
              ['⑧ 尾部相依', '极端分位联合超越 / 领先滞后',
               f'99% 分位联合/独立 = {TAIL["quantiles"]["99"]["ratio"]:.1f}×；峰值在 k=0',
               '<span class="pill p-pass">风控共振，无领先套利</span>']]))
H.append('<div class="box"><b>一句话总括：</b>八根 K 线形态码是一个「低熵、二阶记忆、阴阳分族、有节律、长记忆波动」的系统——'
         '可预测性稀缺但真实（一阶 top-1 24–28% vs 随机 12.5%），且全部集中在幅度维度；'
         '形态码本身的六位里只有约 2.7 比特是有效信息。</div>')

# ---------- D1 ----------
H.append('<h2>二、维度① 信息论与压缩：每根 K 线只讲 2.7 比特的话</h2>')
H.append(fig('entropy'))
H.append(tbl(['指标', 'MA', 'EG', '含义'],
             [['H(当前状态)', f'{e["H_state"]:.3f} bit', f'{ee["H_state"]:.3f} bit', '八态边缘分布的熵'],
              ['H(下一态 | 当前态)', f'{e["block"]["1"]:.3f} bit', f'{ee["block"]["1"]:.3f} bit', '一阶条件熵'],
              ['块熵 k=2', f'{e["block"]["2"]:.3f} bit', f'{ee["block"]["2"]:.3f} bit', '两态记忆'],
              ['块熵 k=3（熵率）', f'<b>{e["block"]["3"]:.3f} bit</b>', f'<b>{ee["block"]["3"]:.3f} bit</b>', '每根实际信息量'],
              ['压缩率', f'<b class="r">{e["compress_ratio"]*100:.1f}%</b>', f'<b class="r">{ee["compress_ratio"]*100:.1f}%</b>', '2.7 / 6 比特'],
              ['有效字母表', f'{e["eff_alphabet"]:.1f} 个等价态', f'{ee["eff_alphabet"]:.1f}', 'e^H，八态实际只当 ~18 个等概态用'],
              ['冗余度', f'{e["redundancy"]:.3f} bit', f'{ee["redundancy"]:.3f} bit', 'H(X) − 熵率']]))
H.append('<div class="box ok"><b>结论：</b>六位编码每根只携带约 <b>2.72 比特</b>（MA）/ <b>2.78 比特</b>（EG）信息，'
         '压缩率 45–46%——超过一半的码位是冗余。分年熵率七年稳定在 2.63–2.80 bit，结构不随时间退化。'
         '工程含义：存储/传输六位码可无损压掉一半；任何基于「8 态等概」的假设（如均匀先验）都高估了信息量。</div>')

# ---------- D2 ----------
H.append('<h2>三、维度② 记忆深度：卦变二阶仍有增量，三阶起饱和</h2>')
H.append(fig('order'))
rows = []
for o in ['0', '1', '2', '3']:
    rows.append([f'{o} 阶马尔可夫', f'{m["holdout"][o]["loss"]:.4f}', f'{mm["holdout"][o]["loss"]:.4f}',
                 f'{m["holdout"][o]["loss"]-m["holdout"][str(int(o)+1)]["loss"]:+.4f}' if o != '3' else '—',
                 f'{mm["holdout"][o]["loss"]-mm["holdout"][str(int(o)+1)]["loss"]:+.4f}' if o != '3' else '—'])
H.append(tbl(['模型（2026 holdout）', 'MA log2损失', 'EG log2损失', 'MA 增益', 'EG 增益'], rows))
H.append(tbl(['准确率口径', 'MA', 'EG'],
             [['一阶模型 top-1 猜中率', f'{m["order1_top1_acc"]*100:.1f}%', f'{mm["order1_top1_acc"]*100:.1f}%'],
              ['持续性基线（猜不变）', f'{m["persistence_acc"]*100:.1f}%', f'{mm["persistence_acc"]*100:.1f}%'],
              ['随机基线（8 选 1）', '12.5%', '12.5%']]))
H.append('<div class="box"><b>结论：</b>记忆以一阶为主（增益 +0.066 bit），二阶仍有实质增量（+0.041 bit），三阶起饱和（+0.013 bit）。'
         '一阶模型的 top-1 猜中率 24.1%（MA）/ 28.2%（EG），显著高于持续性基线（17.5%/19.2%）与随机（12.5%）。'
         '即：<b>「知道当前卦象」确实提高猜中率，但绝对水平仍接近抛硬币</b>——形态信息真实存在，用于过滤与状态识别可以，用于直接预测下一根方向不够。</div>')

# ---------- D3 ----------
H.append('<h2>四、维度③ 图论：转移图的社区就是阴阳两族</h2>')
H.append(fig('graph'))
H.append(tbl(['图论量', 'MA', 'EG', '解读'],
             [['显著边（3σ）', f'{g["n_sig_edges"]}/64（+{g["n_sig_pos"]}/-{g["n_sig_neg"]}）', f'{MA["graph"]["n_sig_edges"]}/64',
               '九成边的偏好/回避显著偏离边缘分布'],
              ['强连通性', '是', '是', '不可约：任意态可达任意态，无吸收态'],
              ['社区划分', '[离艮震坤 | 乾巽兑坎]', '同左', '<b>图拓扑独立复现距离矩阵的两家族</b>'],
              ['模块度 Q', f'{g["modularity"]:.3f}', f'{EG["graph"]["modularity"]:.3f}', '弱社区：家族内偏好强于家族间'],
              ['加权平均路径', f'{g["mean_path"]:.2f} bit', f'{EG["graph"]["mean_path"]:.2f} bit', '从一态「游走」到另一态的平均信息成本'],
              ['PageRank 最高', f'坎☵ {g["pagerank"][7]:.3f}', f'坎☵ {EG["graph"]["pagerank"][7]:.3f}', '高波动双影态是网络的枢纽']]))
H.append('<div class="box ok"><b>结论：</b>把 8×8 转移矩阵当有向图看，社区检测把八态干净地切成「阴族（实体向下四态）」与「阳族（实体向上四态）」——'
         '与距离矩阵聚类逐位一致，两个独立方法互证。PageRank 最高的枢纽是<b>坎☵</b>（双影高波动态），'
         '平均路径仅 ~1.96 bit：这个网络很小、很通达，任何形态一两个转移内就能到任何其他形态。</div>')

# ---------- D4 ----------
H.append('<h2>五、维度④ 频域与节律：周一效应与三月高峰</h2>')
H.append(fig('freq'))
dow_rows = []
dws = ['周一', '周二', '周三', '周四', '周五']
for i in range(5):
    dow_rows.append([dws[i], f'{f["dow_mean"][i]*100:.3f}%', f'{ff["dow_mean"][i]*100:.3f}%'])
H.append('<h3>星期 × 平均振幅</h3>' + tbl(['星期', 'MA', 'EG'], dow_rows))
H.append('<h3>月度平均振幅（%）</h3>' + tbl(
    ['月份'] + [f'{i+1}月' for i in range(12)],
    [['MA'] + [f'{v*100:.2f}' for v in f['month_mean']],
     ['EG'] + [f'{v*100:.2f}' for v in ff['month_mean']]]))
H.append('<div class="box"><b>结论：</b>① <b class="r">周一效应</b>——周一平均振幅只有周二~周五的 '
         f'{f["dow_mean"][0]/sum(f["dow_mean"][1:])*4:.2f} 倍（MA）/ {ff["dow_mean"][0]/sum(ff["dow_mean"][1:])*4:.2f} 倍（EG），'
         '且星期×时段矩阵 σ₁ 占 72%，说明星期效应几乎是单一因子（周一 vs 其余）；'
         '② <b>月令节律</b>——三月振幅最高、八月最低，峰谷比 1.39，F 检验 p&lt;1e-16；'
         '③ 日内谱峰周期 115/172.5 分钟 ≈ 会话长度，节律来自开收盘结构而非神秘周期；'
         '④ 日振幅谱为红噪声型，无可交易的多日周期。<b>用途：仓位日历（周一/八月降波动预期），不是择时信号。</b></div>')

# ---------- D5 ----------
H.append('<h2>六、维度⑤ 结构稳定性：2026 不比任何普通年份更怪</h2>')
H.append(fig('stability'))
fro_rows = []
for y in sorted(int(k) for k in st['Fro_by_year']):
    fro_rows.append([str(y), f'{st["Fro_by_year"][str(y)]:.3f}', f'{se["Fro_by_year"][str(y)]:.3f}'])
H.append(tbl(['年份', 'MA Fro 相对差', 'EG Fro 相对差'], fro_rows))
H.append(tbl(['稳定性检验', 'MA', 'EG'],
             [['2026 vs 训练集 Fro', f'{st["Fro_2026_vs_train"]:.3f}', f'{se["Fro_2026_vs_train"]:.3f}'],
              ['χ² 齐性（年×64格）', f'χ²={st["chi2"]:.0f}, p<1e-16', f'χ²={se["chi2"]:.0f}, p<1e-16'],
              ['2026 惊讶度（log2损失）', f'{st["logloss_2026"]:.4f}（训练 {st["logloss_train"]:.4f}，{st["surprise_gap"]:+.4f}）',
               f'{se["logloss_2026"]:.4f}（训练 {se["logloss_train"]:.4f}，{se["surprise_gap"]:+.4f}）'],
              ['CUSUM 变点', '无持续单侧漂移', '无持续单侧漂移']]))
H.append('<div class="box warn"><b>结论：</b>转移矩阵逐年漂移 0.08–0.33 且 χ² 在 50 万样本下高度显著——'
         '<b>但量级小、无单调趋势、CUSUM 无变点</b>。2026 对训练集的漂移（MA 0.236 / EG 0.247）落在普通年份区间内，'
         '惊讶度一升（MA +0.096 bit）一降（EG −0.068 bit），无系统性退化。'
         '工程含义：<b>用滚动窗口（如近 2 年）重估转移矩阵即可，不需要变点检测机制</b>；'
         '若死用 2020 年的矩阵，每根多付约 0.1 bit 的预测成本。</div>')

# ---------- D6 ----------
H.append('<h2>七、维度⑥ 长程标度：波动长记忆，收益短程回归</h2>')
H.append(fig('scaling'))
vr_rows = []
for k in ['2', '5', '10', '30']:
    if sc['var_ratio'].get(k) is None:
        continue
    vr_rows.append([f'k={k}', f'{sc["var_ratio"][k]:.3f}', f'{sce["var_ratio"][k]:.3f}'])
H.append(tbl(['方差比 VR(k)', 'MA', 'EG'], vr_rows))
acf_rows = []
lags = [str(k) for k in sc['amp_acf']]
for i, k in enumerate(lags):
    acf_rows.append([f'{k} 根', f'{sc["amp_acf"][k]:.3f}', f'{sce["amp_acf"][k]:.3f}'])
H.append(tbl(['振幅自相关（同会话池化）', 'MA', 'EG'], acf_rows))
H.append('<div class="box ok"><b>结论：</b>三个刻度讲同一个故事——<b>振幅（波动率）是长记忆的</b>：'
         f'1 分钟 DFA α={sc["dfa_1m_alpha"]:.2f}（EG {sce["dfa_1m_alpha"]:.2f}，>0.5 即持续性），日振幅 α={sc["dfa_day_alpha"]:.2f}，'
         '振幅自相关 60 根后仍 0.34–0.40；而<b>收益是短程均值回归的</b>：VR(2)=0.87–0.93，VR(10)=0.66–0.75 且随 k 递减。'
         '这正是「波动率聚集」的标度签名，与 V5 的三层讲解、线性代数的 Γ₁ 振幅可预测性（23.6%）互为印证。'
         '注意：会话 ≤120 根，日内 DFA 有效标度只有 4–24 根，α 的绝对值应打折看，符号方向（>0.5）才是稳健结论。</div>')

# ---------- D7 ----------
H.append('<h2>八、维度⑦ 条件结构：综卦「貌离神合」</h2>')
H.append(fig('cond'))
H.append(tbl(['条件分布比较（JS 距离，比特）', 'MA', 'EG', '含义'],
             [['综卦对（影线换边）均值', f'<b class="g">{c["js_zong_mean"]:.4f}</b>', f'<b class="g">{ce["js_zong_mean"]:.4f}</b>', '条件分布几乎相同'],
              ['错卦对（三爻全反）均值', f'<b class="r">{c["js_cuo_mean"]:.4f}</b>', f'<b class="r">{ce["js_cuo_mean"]:.4f}</b>', '条件分布明显不同'],
              ['各态下一根振幅集中度', f'{min(c["row_max_amp"])*100:.0f}–{max(c["row_max_amp"])*100:.0f}%',
               f'{min(ce["row_max_amp"])*100:.0f}–{max(ce["row_max_amp"])*100:.0f}%', '最高档也只 21–36%，接近边缘分布 20%']]))
H.append('<div class="box bad"><b>结论（推翻前论）：</b>V2/矩阵视角曾判「综卦=大变动」——因为综卦对在特征空间距离 3.28，'
         '比平均 2.89 还远。但条件分布给出相反答案：<b>综卦对的下一根振幅分布 JS 距离仅 0.001–0.002 比特，比错卦对近 30 倍</b>。'
         '静态几何（当前长什么样）与动态几何（之后怎么走）是两个问题；对预测而言，<b>动态口径才该信</b>——'
         '影线换边不改变后续动力学，「上下影互换」在演化意义上近似同一个状态的两种观测。</div>')

# ---------- D8 ----------
H.append('<h2>九、维度⑧ 尾部相依：极端行情同生共死</h2>')
H.append(fig('tail'))
tq = TAIL['quantiles']
H.append(tbl(['分位', '联合超越', '独立假设', '倍数', 'P(MA极端|EG极端)', 'P(EG极端|MA极端)'],
             [[f'>{q} 分位', f'{tq[q]["joint"]*100:.2f}%', f'{tq[q]["indep"]*100:.2f}%',
               f'<b class="r">{tq[q]["ratio"]:.1f}×</b>', f'{tq[q]["cond_MA_given_EG"]*100:.0f}%',
               f'{tq[q]["cond_EG_given_MA"]*100:.0f}%'] for q in ['90', '95', '99']]))
H.append(tbl(['滞后 k（分钟）'] + [f'{k:+d}' if k else '0' for k in sorted(int(x) for x in TAIL['lead'])],
             [['95% 尾部联合'] + [f'{TAIL["lead"][str(k)]*100:.2f}' for k in sorted(int(x) for x in TAIL['lead'])]]))
H.append('<div class="box bad"><b>结论：</b>MA 与 EG 的极端波动强烈共尾：99% 分位的联合超越概率是独立假设的 '
         f'<b>{tq["99"]["ratio"]:.0f} 倍</b>（0.34% vs 0.01%），90% 分位也有 {tq["90"]["ratio"]:.1f} 倍；'
         '且领先滞后峰值钉在 k=0，±5 分钟衰减约 30%。<b>两个品种不是分散化对象，而是同一个风险因子的两个读数</b>——'
         '与收益互相关（同刻 0.44）、联合 MI（0.086 bit）结论一致。工程上直接用作风控共振探测器：'
         '同板块品种同时亮高波动态 ⇒ 联动降仓，而不是各算各的止损。</div>')

# ---------- 交叉验证 ----------
H.append('<h2>十、交叉验证：新维度与既有结论的互证网</h2>')
H.append(tbl(['新维度结论', '印证/推翻的既有结论', '关系'],
             [['熵率 2.72 bit、压缩率 45%', 'V6 code7：六位码在振幅维只捕获 11.9%', '印证（信息论版）'],
              ['一阶 top-1 24–28% vs 随机 12.5%', 'V3：八态链是合格马尔可夫链', '印证'],
              ['图社区 = 阴4态|阳4态', '矩阵视角：距离矩阵两家族聚类', '独立方法互证'],
              ['转移图 59/64 边显著、不可约', 'V1：混合时间 1.1 根、无吸收态', '印证'],
              ['DFA α>0.5、VR<1、ACF 衰减慢', 'V5 波动率聚集 + 线性代数 Γ₁ 振幅可预测 23.6%', '印证（标度版）'],
              ['周一振幅低 31%、三月最高', 'V4 日×时段矩阵 σ₁=72.5% 可分离结构', '延伸（新增日历因子）'],
              ['2026 漂移在普通年份区间内', 'V4：2026 holdout 规律存活', '印证'],
              ['<b>综卦条件分布几乎相同（JS 0.001）</b>', '<b>V2/矩阵视角「综卦=大变动」（距离 3.28）</b>', '<b>推翻（静态口径误导）</b>'],
              ['尾部相依 34×、峰值 k=0', '矩阵视角：MA×EG 同步共动无领先', '印证（极端版）']]))

# ---------- 工程结论 ----------
H.append('<h2>十一、工程结论（七条）</h2>')
H.append('<div class="box"><b>1. 存储与传输：</b>六位码可无损压缩约 55%（熵率 2.72/6 bit），按 8 态等概设计的任何先验或采样方案都会高估信息量。<br>'
         '<b>2. 状态识别可用、方向预测不可用：</b>一阶猜中率 24–28% 显著高于基线，但绝对水平接近抛硬币；形态码适合做<b>市场状态分类器与过滤器</b>，不适合做方向信号。<br>'
         '<b>3. 二阶记忆值得建模：</b>2 阶马尔可夫比 1 阶再省 0.041 bit/根（约 1.4% 预测成本），3 阶起饱和——实现取 2 阶即可。<br>'
         '<b>4. 仓位日历：</b>周一降波动预期（振幅仅周二~周五的 0.69 倍）、三月高波动、八月低波动，纳入仓位/止损的日期标定。<br>'
         '<b>5. 风控共振：</b>MA/EG 极端波动共尾 34 倍且同步——同板块联动降仓规则；不要做跨品种分钟级领先套利（不存在）。<br>'
         '<b>6. 滚动重估：</b>转移矩阵年漂移 0.08–0.33、2026 无异常——用近 2 年滚动窗口估计即可，无需变点检测。<br>'
         '<b>7. 标度意识：</b>波动长记忆（DFA α≈0.8）+ 收益短程回归（VR&lt;1）⇒ 止损/加仓的时间标度应匹配：'
         '幅度类决策（仓位、止损宽度）看波动聚集，方向类决策看短程回归，不要混用。</div>')

H.append('<h2>十二、口径与边界</h2>')
H.append('<div class="box"><b>口径：</b>① 编码为 实体方向2位+上影2位+下影2位（00=无/跌，01=有/涨），加 open/chg/amp 构成四元组；'
         '② 相邻对限定会话内 60 秒（夜盘归属次日交易日）；③ 训练 2020-2025（489,975 根/品种），holdout 2026（35,865 根/品种）；'
         '④ 日内 DFA 受会话 ≤120 根限制，有效标度 4–24 根，α 的绝对值打折看、方向看；'
         '⑤ 全部结论经 MA/EG 双品种交叉验证，方向一致。<br><br>'
         '<b>边界声明：</b>本报告为期货 K 线形态编码的统计分析，属传统文化（八卦编码）与量化研究的结合探索，'
         '结论仅供研究参考，不构成任何投资建议；实际交易请以正规行情数据、专业机构意见与自身风险承受能力为准。</div>')
H.append('<div class="mut">生成于 2026-10-17 · 数据：MA/EG 主力连续 1 分钟 2020-2026 · 脚本：step24_dimensions.py / step25_dim_figs.py / step26_report_dimensions.py</div>')
H.append('</div></body></html>')

open('dimensions_report.html', 'w', encoding='utf-8').write(''.join(H))
print('dimensions_report.html 已生成, 大小', len(''.join(H)), '字节')
