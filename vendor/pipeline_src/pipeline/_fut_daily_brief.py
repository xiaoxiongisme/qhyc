# 国内期货多空策略简报 —— 每日流水线（可执行点位版）
# 输入：_fut_universe_final.csv（49 个高流动性主力合约）
#        E:\QH\期货简报\持仓情况和手续费\_positions.csv   （持仓台账，用户可编辑；脚本只刷新行情派生列）
#        E:\QH\期货简报\持仓情况和手续费\_fee_config.csv  （手续费与账户参数，用户可编辑）
# 流程（两步）：
#   ① python _fut_daily_brief.py            → 抓日线算指标，导出 _brief_tech_YYYYMMDD.json + 动作快照
#   ② python _fut_daily_brief.py --render   → 渲染简报 MD（一总览 / 二多头 / 三空头 / 四持仓 / 五纪律 / 附·本期主线+护栏）
#
# 关键口径（用户规则）：
#   · 所有点位以**当日收盘价**界定，次日开盘执行（不盘中抢跑）
#   · 所有品种统一走「融合策略 V2.0」引擎（额外执行口径），总览表与二三章标注「融合」
#   · 持仓台账以用户编辑为准，未编辑的字段保持不变
#   · 手续费计入持仓浮盈（净浮盈 = 毛盈亏 - 开仓手续费 - 平仓手续费）
import os, sys, time, datetime, traceback, json, socket, glob
import pandas as pd
import numpy as np
try:
    import akshare as ak
except Exception:
    ak = None  # 仅 fetch_series 需要 akshare；纯指标/回测（用缓存数据）可在无 akshare 环境运行

socket.setdefaulttimeout(20)

WS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WS)
from _fut_specs import FUT_SPECS
import _fut_positions as POS
from _fut_active_contract import active_contract, rollover_note   # M2 P1：活跃合约解析（合约级数据层）

UNIV = os.path.join(WS, "_fut_universe_final.csv")
# 输出根目录迁移至 E:\QH\期货简报\（2026-09-04 调整）：不同文件类型分流到 4 个子目录
BASE_DIR = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")
BRIEF_DIR = os.path.join(BASE_DIR, "简报内容")          # 最终简报 md
HOLD_DIR = os.path.join(BASE_DIR, "持仓情况和手续费")  # _positions.csv / _fee_config.csv / _brief_actions_*.json
PROC_DIR = os.path.join(BASE_DIR, "过程思考")          # _brief_tech_*.json / _brief_fund_*.json（可定期清理）
os.makedirs(BRIEF_DIR, exist_ok=True)
os.makedirs(HOLD_DIR, exist_ok=True)
os.makedirs(PROC_DIR, exist_ok=True)
# 向后兼容：保留 OUT_DIR 名称（指向 HOLD_DIR，旧代码若引用 _brief_actions 会正确解析）
OUT_DIR = HOLD_DIR
os.makedirs(OUT_DIR, exist_ok=True)

ADX_THR = 22          # 通用趋势市门槛（仅用于「趋势」列标注；融合方向层以 EMA20 为准，不再作入场硬门槛）
ACCOUNT = 150000.0     # M2 P1：本金 15w（兜底常量；实际以 _fee_config.csv 账户行为准）
RISK = 0.04            # M2 P1：单笔风险 4%（兜底常量）

# ---------- 融合策略 V2.0（额外执行口径）核心参数 ----------
# 方向层 = 日线 EMA20（≈ 小时 EMA140）；入场 = A 回踩重启 / B 10根突破；
# 离场 = 2×ATR 初始 + 2×ATR 吊灯 + 0.5R 保本；仅收盘价击穿才平仓（收盘价盯盘铁律）
SLK = 2.0             # 初始止损 = 入场 ∓ 2×ATR
TRK = 2.0             # 吊灯移动止损 = 自极值回撤 2×ATR 离场（无固止盈）
BER = 0.5             # 保本触发 = 浮盈 ≥ 0.5×ATR 时止损上移至入场价
FUSION_ENGINE = "融合"   # 统一引擎标注（已退役 V1/V3/V4 派发）
MAX_POS = 8                                      # M2 P1：仓位纪律：最多同时持仓个数（限仓）
SECTOR_CAP = 0                                   # 板块限额（0=不启）：实测近似零贡献且与时间止损叠加略负（§16.6 归因），默认关；设 2 启用
MAX_HOLD_BARS = 30                               # v2 时间止损：持仓满 30 根 K 线未达预期→止损收紧至保本（与回测 --time-stop 30 一致）
GATE = 0.35                                      # 趋势市门槛：全样本趋势(多/空)占比低于此值则封印新开仓（与回测 --gate 一致）

# ---------- V3.4 门槛口径（对齐 app/core/config.py::fusion.*；技术债清偿见 PRD §16）----------
# 简报侧原停在 V2.0 底座，缺少 ADX 门控与斐波汇流；本组常量补齐「与周期无关的门槛口径」。
ADX_GATE = 15.0       # V3.1：融合入场硬门槛（= fusion.adx_min）；用「上一根」已收盘 ADX 判定
FIB_CONFL = True      # V3.2：斐波那契·汇流门控（= fusion.fib_confl）；仅约束 A 回踩支路
FIB_W = 60            # 斐波回看窗口（根）= fusion.W
FIB_RATIOS = (0.382, 0.5, 0.618)   # 斐波档位 = fusion.fib_ratios
USE_SBULL = False     # V3.1：回踩支路是否要求「收强/收弱」（= fusion.use_sbull）；False=取消（⑸ 对齐）
FIB_TOL_ATR = 0.5     # 档位容差 = fusion.fib_tol_atr（单位：×ATR）
# ---------- V3.4+ 斐波腿定义放宽（PRD §16.9，2026-09-22，**仅技能侧**）----------
# 🔴 2026-09-22 晚：实测后**已按用户决定回退**（禁用放宽，恢复旧口径）。
# 依据：_piv_gap_probe.py 实测 1889 例 A 触发中「无有效腿」704 例（37.3%），
#       100% 由旧 `(i-piv) < 2` 造成；枢轴距离中位数恰为 2、gap==1 最大单档。
# 放宽（FIB_MIN_GAP=1 + FIB_K_ATR=1.0）双跑结果：信号 +6.82%、但
#       胜率 31.55%→31.46%、盈亏比 2.120→2.100、期望 −3.55→−5.64 pt/笔（恶化 59%）
#       → **放进来的是负期望交易**，与 §16.8「斐波=风险调节器」定位一致 → **维持现状**。
# 结论全文：过程思考/PRD16_斐波腿定义放宽评估_20260922.md；PRD §16.9。
# ⚠️ 放宽代码仍保留（未删除），仅由下面两个常量关闭，便于将来复测/扫描 k：
#       放宽 = FIB_MIN_GAP 1 + FIB_K_ATR 1.0 ｜ 回退（当前） = 2 + 0
# 回退等价性已验收：_recon_engine.py 676 组合 → 与线上实现**逐位一致**（不一致 0）。
FIB_MIN_GAP = 2       # 枢轴与当根的最小间隔（旧口径 2；放宽为 1）
FIB_K_ATR = 0.0       # 腿长下限 = FIB_K_ATR × ATR；0 = 不设幅度门槛（关闭放宽）
ADDON_SEQ_BARS = 120  # 加仓判定内嵌的日线序列长度（供台账「收盘创入场以来新高」比对）

# ---------- 锁仓机制（v2.1 生产默认，与回测 --lock-* / _fut_positions.LOCK_* 保持同步） ----------
# 回测验证（§18）：锁 t0.8 全样本 +680,746（修复基线 +484,559，Δ+196,187）、峰回撤 31.3%；
# OOS 8 窗口 7/8 更优、均值 +334,775 vs +236,600。锁早（≤0.5×ATR）冻结本会自愈的浮亏=净伤害。
LOCK_MAX = 1          # 最大并发锁仓品种数（0=不启用，回退 v2f 口径）
LOCK_TRIG = 0.8       # 锁仓触发：原仓浮亏 ≥ 0.8×ATR 且信号未反转/未转弱才锁
LOCK_UNLOCK = 0.0     # 解锁：组合浮盈回补至 ≥0 元（保本）即平锁仓腿，原仓恢复原止损/止盈
LOCK_MAX_TRIES = 2    # 同一持仓最多锁仓尝试次数（简报层不精确计数，靠纪律；回测 74 周期 0 次耗尽）

# 板块映射（与 _fut_backtest.py::SECTOR 保持同步；勿在简报直接 import _fut_backtest——存在循环依赖）
SECTOR = {}
for _r in ("RB", "HC", "I", "J", "JM"):
    SECTOR[_r] = "黑色系"
for _r in ("CU", "AL", "ZN", "NI", "SN", "PB", "AO"):
    SECTOR[_r] = "有色金属"
for _r in ("AU", "AG"):
    SECTOR[_r] = "贵金属"
for _r in ("SC", "FU", "BU", "LU"):
    SECTOR[_r] = "能源"
for _r in ("RU", "NR", "BR", "SP", "SS", "L", "V", "PP", "EG", "EB",
           "TA", "MA", "FG", "SA", "UR", "PF", "SH", "PX", "PG"):
    SECTOR[_r] = "化工"
for _r in ("Y", "P", "M", "RM", "OI", "A", "B", "CS", "PK"):
    SECTOR[_r] = "油粕农产品"
for _r in ("CF", "SR", "JD", "LH", "AP", "CJ", "C"):
    SECTOR[_r] = "软商品"
for _r in ("SI", "LC", "PS"):
    SECTOR[_r] = "新能源"
# （旧 INCLUDE_V1 开关已随 V1 引擎退役一并移除；融合引擎统一纳入可操作池）

# ---------- 护栏（保留 prudent 风险过滤；V3/V4 专属 gate 随引擎退役） ----------
# (i) |20日涨跌| > 15% 抑制新开，避免趋势衰竭点追顶/抄底——对应棉花(+8%) 追高回落。
GUARD_EXHAUST = True    # 趋势衰竭过滤（|p20| > EXT_TREND 不扣扳机）
EXT_TREND = 0.15         # 20日收益率阈值

# 护栏(iii) — 入场警示（简报层 · 不强制禁入）
# 引擎信号给出后，若当前收盘已远离 10 日突破点 K×ATR / 2%，
# 简报给出"⚠️延伸警示"文案让用户明日开盘警惕同向追高跳空；
# 但 action 仍保留可操作——是否最终入场由实盘层执行跳空机制判定。
# 实盘对应机制见 _fut_backtest.py 的 --gap-chase（默认关闭，需显式开启）。
GUARD_ENTRY_ZONE = True      # 启用入场警示（仅文案，不禁入）
ENTRY_CHASE_ATR = 1.0        # 距突破点 > K×ATR 时给出警示（默认 1×ATR）
ENTRY_CHASE_PCT = 0.02       # 距突破点 > 2%（绝对值维度）也给出警示

# ---------- 引擎：融合策略 V2.0 统一引擎（2026-09-14 全量切换） ----------
# 原 V4/V3/V1「按品种派发」已整体退役；所有品种统一走融合策略（额外执行口径）：
#   方向层：日线 EMA20（≈ 小时 EMA140）—— 只做顺势一侧；
#   入场：A 回踩重启（优先）/ B 10 根突破（补位）；
#   离场：2×ATR 初始止损 + 2×ATR 吊灯移动止损 + 0.5R 保本，仅收盘价击穿才平仓。
def engine_of(root=None):
    """【遗留兼容】原 V1/V3/V4 派发映射，仅供回测/研究脚本使用；每日简报不使用本函数。"""
    return ENGINE.get(root, "V1")


# =====================================================================
# 【遗留兼容块 · 仅供回测/研究脚本 import；每日简报已全量切换为融合引擎】
# 使用方：_fut_backtest.py / _fut_pervar.py / _fut_dual_engine.py /
#         _fut_threshold_test.py / _fut_guard_confirm.py 等历史脚本。
# 这些符号**不参与** fusion_signal 与 render 的每日简报生成（简报统一走融合）。
# =====================================================================
ADX_ENGINE = 25       # V3/V4 入场 ADX 门槛（遗留）
ENGINE_TP = {"V4": 1.5, "V3": 1.5, "V1": 4.0}
ENGINE_TRAIL = {"V4": 3.0, "V3": 3.0, "V1": 1.0}
ENGINE_SL = 1.0
GUARD_V3_BEAR = True
ENGINE = {
    'A': 'V4', 'AL': 'V4', 'AP': 'V4', 'CF': 'V4', 'FU': 'V4', 'HC': 'V4', 'JD': 'V4', 'JM': 'V4',
    'LC': 'V4', 'LH': 'V4', 'M': 'V4', 'PP': 'V4', 'SA': 'V4', 'SM': 'V4', 'SS': 'V4', 'Y': 'V4',
    'AO': 'V3', 'C': 'V3', 'CS': 'V3', 'EB': 'V3', 'EG': 'V3', 'L': 'V3', 'MA': 'V3', 'PF': 'V3',
    'PS': 'V3', 'PX': 'V3', 'RU': 'V3', 'SI': 'V3', 'SP': 'V3', 'TA': 'V3', 'UR': 'V3', 'V': 'V3',
    'B': 'V1', 'BR': 'V1', 'BU': 'V1', 'CJ': 'V1', 'FG': 'V1', 'I': 'V1', 'NI': 'V1', 'OI': 'V1',
    'P': 'V1', 'PG': 'V1', 'PK': 'V1', 'RB': 'V1', 'RM': 'V1', 'SF': 'V1', 'SH': 'V1', 'SR': 'V1',
    'ZN': 'V1',
}


def engine_signal(met):
    """【遗留兼容】原 V1/V3/V4 引擎信号，仅供回测/研究脚本调用；每日简报改用 fusion_signal。"""
    root = met["root"]; eng = engine_of(root)
    close = met["close"]; atr_v = met["atr"]; adx_v = met["adx"]
    ma20, ma60, ma250 = met["ma20"], met["ma60"], met.get("ma250")
    ma250 = ma250 if ma250 else met["ma120"]
    met["engine"] = eng
    met["regime"] = "多头市" if close > ma250 else "空头市"
    met["ma250"] = ma250
    met["tp_mult"] = ENGINE_TP[eng]
    met["sl_mult"] = ENGINE_SL

    pb_long = (ma20 > ma60) and (close > ma60) and (close < ma20)
    pb_short = (ma20 < ma60) and (close < ma60) and (close > ma20)
    base = adx_v >= ADX_ENGINE
    why_block = None
    if eng == "V4":
        if close > ma250:
            do_long = base; do_short = base and pb_long
        else:
            do_short = base; do_long = base and pb_short
    elif eng == "V3":
        do_long = base and pb_long
        do_short = base
        if GUARD_V3_BEAR:
            if do_short and not (close < ma250):
                why_block = (f"护栏(i)拦截：V3 仅空头市做空，当前收盘 {close:,.0f} > MA250（多头市），"
                             f"禁止上涨市裸空；做多需回踩 MA20~MA60 确认")
            do_short = do_short and (close < ma250)
    else:
        do_long = (met["trend"] == "多头") and (adx_v >= ADX_THR) and met.get("brk_up", False)
        do_short = (met["trend"] == "空头") and (adx_v >= ADX_THR) and met.get("brk_dn", False)

    if GUARD_EXHAUST:
        _p20 = met.get("p20", 0.0) or 0.0
        if abs(_p20) > EXT_TREND and (do_long or do_short):
            why_block = (f"护栏(ii)拦截：|20日涨跌| {abs(_p20)*100:.0f}% > {EXT_TREND*100:.0f}%，"
                         f"趋势衰竭，不追顶/抄底")
            do_long = do_short = False

    if do_long and do_short:
        do_long = close > ma250
        do_short = not do_long

    if eng == "V4":
        side_long = "顺势做多（收盘>MA250）" if close > ma250 else "逆势做多·回踩确认"
        side_short = "顺势做空（收盘<MA250）" if close <= ma250 else "逆势做空·回踩确认"
    elif eng == "V3":
        side_long = "回踩确认做多"
        side_short = "顺势做空" if close <= ma250 else "偏空做空（逆势·引擎仅做空）"
    else:
        side_long = "10日突破做多"
        side_short = "10日破位做空"

    if do_long:
        met["action"] = "做多"; met["entry"] = close
        met["stop"] = close - ENGINE_SL * atr_v
        met["target"] = close + ENGINE_TP[eng] * atr_v
        met["trail"] = ENGINE_TRAIL[eng] * atr_v
        met["reason"] = f"ADX={adx_v:.0f}，{eng} {side_long}"
    elif do_short:
        met["action"] = "做空"; met["entry"] = close
        met["stop"] = close + ENGINE_SL * atr_v
        met["target"] = close - ENGINE_TP[eng] * atr_v
        met["trail"] = ENGINE_TRAIL[eng] * atr_v
        met["reason"] = f"ADX={adx_v:.0f}，{eng} {side_short}"
    else:
        met["action"] = "观望"
        met["entry"] = None; met["stop"] = None; met["target"] = None
        met["trail"] = ENGINE_TRAIL[eng] * atr_v
        if why_block is not None:
            met["reason"] = why_block
        elif adx_v < ADX_THR:
            met["reason"] = f"ADX={adx_v:.0f}<{ADX_THR} 震荡市，不扣扳机"
        elif eng in ("V4", "V3") and adx_v < ADX_ENGINE:
            met["reason"] = f"ADX={adx_v:.0f}<{ADX_ENGINE}，未达{eng}门槛"
        elif eng == "V4":
            met["reason"] = f"{met['regime']}，未满足顺势/回踩确认"
        elif eng == "V3":
            met["reason"] = "多头需回踩 MA20~MA60 确认，当前不满足"
        else:
            met["reason"] = "未满足 趋势+ADX≥22+10日突破"

    met["conviction"] = conviction_score(met)
    met["score"] = met["conviction"]
    return met


# ---------- 指标 ----------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    # D2（PRD16 对账 2026-09-22）：改 Wilder RMA（α=1/n），与线上引擎 atr14 同口径。
    # 原 ewm(span=n) 的 α=2/(n+1) 与 Wilder 相差 1.87×，ATR 中位偏差 4.25%、p95 15.24%，
    # 导致止损幅度（入场 ∓ 2×ATR）同步失真。
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()

def adx(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    # D1（PRD16 对账 2026-09-22）：−DM 必须是 low[i-1] - low[i]（Wilder 标准）。
    # 原 l.diff() = low - low[1] 符号相反 → mdi 错 → dx 错 → adx 错，
    # 实测使 ADX_GATE=15 通过率 53%→92%（误放 48.84% bar）、pdi/mdi 翻转 41%。
    # 注意：DI/ADX 的平滑仍用 ewm(span=n)——与线上引擎 adx14 同式（双方共有偏差，非分叉）。
    up = h.diff(); dn = l.shift(1) - l
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_ = pd.Series(tr).ewm(span=n, adjust=False).mean()
    plus_di = pd.Series(plus_dm).ewm(span=n, adjust=False).mean() / atr_ * 100
    minus_di = pd.Series(minus_dm).ewm(span=n, adjust=False).mean() / atr_ * 100
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).abs() * 100
    return dx.ewm(span=n, adjust=False).mean(), plus_di, minus_di

def donchian_pos(df, n=20):
    hi = df["high"].rolling(n).max(); lo = df["low"].rolling(n).min()
    return (df["close"] - lo) / (hi - lo)

def macd(df, fast=12, slow=26, sig=9):
    ef, es = ema(df["close"], fast), ema(df["close"], slow)
    dif = ef - es; dea = ema(dif, sig); bar = (dif - dea) * 2
    return dif, dea, bar


def conviction_score(met):
    """信号置信度（0~100），每日简报「限仓排序」与回测「按置信度取前 N 开仓」共用同一口径。
    构成：ADX 趋势强度(≤40) + 趋势方向(多头/空头+30、震荡+10) + Donchian 位置(≤10)
          + 20日动量(≤±10) + 同向突破加成(+5)。值越高=趋势越干净、越该优先执行。"""
    adx_v = met.get("adx") or 0.0
    trend = met.get("trend", "震荡")
    dpos = met.get("dpos", 0.0) or 0.0
    p20 = met.get("p20", 0.0) or 0.0
    act = met.get("action")
    s = min(adx_v, 50) / 50 * 40
    if trend == "多头":
        s += 30 + min(max(dpos, 0), 1.5) / 1.5 * 10
    elif trend == "空头":
        s += 30 + min(max(-dpos, 0), 1.5) / 1.5 * 10
    else:
        s += 10
    s += max(-10, min(10, p20 * 100 / 3))
    if act == "做多" and met.get("brk_up"):
        s += 5
    elif act == "做空" and met.get("brk_dn"):
        s += 5
    return round(max(0, min(100, s)), 1)


# =====================================================================
# V3.2 斐波那契·汇流（仅作用于 A 回踩支路）
# 严格对齐 app/strategies/fusion_signal.py 与 _fusion_v33.bt33（逐字同式），
# 详见 PRD §16.3。全部判定只用 i 及以前的数据 → 天然无前视。
# =====================================================================
def _fib_eval(h, l, i, is_long, W, ratios, tol, k_atr=0.0, atr_v=0.0, min_gap=None):
    """在 bar i 处求「最近一个已确认枢轴 → 其后极值」的斐波腿并判定本根是否落在档位内。

    · 枢轴右侧确认：多头 l[j] < l[j-1] 且 l[j] < l[j+1]（空头镜像用 high）
    · 窗口裁剪：仅保留 idx >= i - W 的枢轴（= 线上引擎 while ph[0] < i-W: pop）
    · 多头腿 = 最近一个已确认枢轴低 → h[piv : i+1] 的最高点；档位 = leg_hi − r×(leg_hi−leg_lo)
    · 判定 |l[i] − 档位| <= tol（空头用 h[i]）
    返回 None 表示「无有效腿/窗口不足」，调用方据此作废 A 支路。

    **2026-09-22 §16.9 放宽（腿定义改为按幅度）**：
      · 原实现要求 `(i - piv) >= 2`。实测 1889 例 A 触发中「无有效腿」704 例
        **100% 由此造成**（piv is None=0、lg<=0=0）；枢轴距离中位数恰为 2、
        gap==1 是最大单档（704/1889=37.3%）。机理：A 回踩当根低点常即全窗最低
        →枢轴必落 i-1→被判「太近」自锁。
      · 现降为 `(i - piv) >= 1`，并改用**幅度**定义腿的有效性：腿长须
        `lg >= k_atr × atr_v`（k_atr>0 时启用）。腿长不足则**继续往前找更早枢轴**，
        而非直接作废。k_atr<=0 时退化为「不设幅度门槛」（= 纯放宽，仅作对照）。
      · 回退开关：`FIB_MIN_GAP=2` + `FIB_K_ATR=0` 即回旧行为（**逐位一致**，见下）。
    """
    if i < 2:
        return None
    min_gap = int(FIB_MIN_GAP if min_gap is None else min_gap)
    strict_gap = (min_gap >= 2)     # 旧口径：最近枢轴不合规即由自身作废（不回溯前寻）
    use_amp = bool(k_atr and k_atr > 0 and atr_v and np.isfinite(atr_v) and atr_v > 0)
    amp_thr = (float(k_atr) * float(atr_v)) if use_amp else 0.0
    lo_bound = max(0, i - int(W))
    piv = None
    for j in range(i - 1, lo_bound - 1, -1):
        if j - 1 < 0:
            continue
        if is_long:
            if not (l[j] < l[j - 1] and l[j] < l[j + 1]):
                continue
            if strict_gap and (i - j) < min_gap:
                break               # 旧口径：最近枢轴太近 → 整根作废（不回退前寻）
            if (i - j) < min_gap:
                continue
            seg = h[j:i + 1]
            if len(seg) == 0:
                continue
            _hi = float(seg.max()); _lo = float(l[j]); _lg = _hi - _lo
        else:
            if not (h[j] > h[j - 1] and h[j] > h[j + 1]):
                continue
            if strict_gap and (i - j) < min_gap:
                break
            if (i - j) < min_gap:
                continue
            seg = l[j:i + 1]
            if len(seg) == 0:
                continue
            _hi = float(h[j]); _lo = float(seg.min()); _lg = _hi - _lo
        if _lg <= 0:
            continue
        if use_amp and _lg < amp_thr:
            continue          # 腿太短 → 换更早的枢轴（幅度定义腿）
        piv = j
        break
    if piv is None:
        return None
    if is_long:
        seg = h[piv:i + 1]
        leg_hi = float(seg.max()); leg_lo = float(l[piv])
        lg = leg_hi - leg_lo
        levels = [leg_hi - r * lg for r in ratios]
        px = float(l[i])
    else:
        seg = l[piv:i + 1]
        leg_hi = float(h[piv]); leg_lo = float(seg.min())
        lg = leg_hi - leg_lo
        levels = [leg_lo + r * lg for r in ratios]
        px = float(h[i])
    k = min(range(len(levels)), key=lambda q: abs(px - levels[q]))
    hit = bool(abs(px - levels[k]) <= tol)
    return dict(pivot=int(piv), leg_hi=leg_hi, leg_lo=leg_lo,
                levels=[float(x) for x in levels], hit=(int(k) if hit else None), ok=hit)


def fib_confluence(df, direction, atr_v, W=60, ratios=(0.382, 0.5, 0.618), tol_atr=0.5):
    """V3.2 斐波那契·汇流门控（仅 A 回踩支路；B 突破不适用）。

    返回 dict(ok, pivot, levels, hit, leg_hi, leg_lo)。语义：
      · direction 为「震荡」→ ok=True（A 支路本就不可用，不作斐波判定）；
      · ATR 取不到（tol<=0）或无有效腿 → ok=False（**保守作废 A 支路，不猜测放行**）。
    """
    out = dict(ok=True, pivot=None, levels=[], hit=None, leg_hi=None, leg_lo=None)
    if df is None or len(df) < 3:
        return out
    d = str(direction)
    if d not in ("多头", "空头"):
        return out
    try:
        h = df["high"].astype(float).to_numpy()
        l = df["low"].astype(float).to_numpy()
    except Exception:
        return out
    tol = float(tol_atr) * float(atr_v or 0.0)
    if not np.isfinite(tol) or tol <= 0:
        out["ok"] = False
        return out
    r = _fib_eval(h, l, len(h) - 1, d == "多头", W, tuple(ratios), tol,
                  k_atr=FIB_K_ATR, atr_v=float(atr_v or 0.0))
    if r is None:
        out["ok"] = False
        return out
    out.update(ok=r["ok"], pivot=r["pivot"], levels=r["levels"], hit=r["hit"],
               leg_hi=r["leg_hi"], leg_lo=r["leg_lo"])
    return out


# ---------- 融合策略信号：给定指标，判定今日是否可操作 + 点位 ----------
def fusion_signal(met):
    """在 met 上写入 engine / action / entry / stop / target / breakeven / trail / reason / stage。
    融合策略 V3.4（额外执行口径）：
      方向层 = 日线 EMA20（≈ 小时 EMA140，用 REF 已收盘值）；
      门控   = ADX(14)≥15（**上一根**判定）+ 斐波汇流（仅 A 回踩支路）；
      入场 = A 回踩重启（优先）/ B 10 根突破（补位）；
      离场 = 2×ATR 初始 + 2×ATR 吊灯 + 0.5R 保本，仅收盘价击穿才平仓；
      加码 = 阶梯加码（浮盈 ≥1.0×ATR 且收盘创入场以来新高 → +1 手，至多 2 手）。"""
    root = met["root"]
    close = met["close"]; atr_v = met["atr"]; adx_v = met.get("adx") or 0.0
    ma20, ma60 = met["ma20"], met["ma60"]
    ma250 = met.get("ma250") or met.get("ma120")
    met["engine"] = FUSION_ENGINE
    met["ma250"] = ma250
    met["tp_mult"] = None          # 融合无固止盈（吊灯移动）
    met["sl_mult"] = SLK

    ema_s = met.get("ema20"); ema_s_prev = met.get("ema20_prev"); close_prev = met.get("close_prev")   # 慢线 = 方向层
    ema_f = met.get("ema10"); ema_f_prev = met.get("ema10_prev")                                       # 快线 = 入场层
    c = close; o = met.get("open", close); h = met.get("high", close); l = met.get("low", close)

    # --- 方向层（慢线 EMA20 ≈ 小时 EMA140；REF 已收盘值，避免用未收盘）---
    _hs = (ema_s is not None) and (ema_s_prev is not None) and (close_prev is not None)
    ok_long = _hs and (close_prev > ema_s_prev)
    ok_short = _hs and (close_prev < ema_s_prev)
    met["direction"] = "多头" if ok_long else ("空头" if ok_short else "震荡")

    # --- 入场 A 回踩重启（快线 EMA10 ≈ 小时 EMA20）：价格回踩快线后被收回 ---
    # --- 入场 B 10 根突破（补位）---
    _hf = (ema_f is not None) and (ema_f_prev is not None) and (close_prev is not None)
    up = _hf and (c > ema_f) and (close_prev < ema_f_prev)     # 上穿快线
    dn = _hf and (c < ema_f) and (close_prev > ema_f_prev)     # 下穿快线
    strb = (c > o) and (c >= (h + l) / 2)                       # 收强
    strr = (c < o) and (c <= (h + l) / 2)                       # 收弱
    # ⑸（PRD16 对账 2026-09-22）：与线上 V3.1 对齐——USE_SBULL=False 时不要求收强/收弱。
    # 原实现硬性要求收强，比线上多一道门，与 V3.1 实证（取消收强更优）矛盾。
    a_long = ok_long and _hf and (c > ema_f) and (l <= ema_f) and (strb or not USE_SBULL) and up
    a_short = ok_short and _hf and (c < ema_f) and (h >= ema_f) and (strr or not USE_SBULL) and dn
    # --- V3.2 斐波那契·汇流门控：**仅约束 A 回踩支路**（B 突破支路不受影响）---
    # 本根逆势极值须落在 0.382/0.5/0.618 档 ±FIB_TOL_ATR×ATR 内；取不到腿/ATR → 保守作废 A。
    if FIB_CONFL:
        _fib_ok = bool(met.get("fib_ok", True))
        a_long = a_long and _fib_ok
        a_short = a_short and _fib_ok
    hh10 = met.get("hh10"); ll10 = met.get("ll10")
    b_long = ok_long and (hh10 is not None) and (c > hh10) and (c > o)
    b_short = ok_short and (ll10 is not None) and (c < ll10) and (c < o)
    # --- V3.1 ADX(14)≥15 趋势强度门控：用「上一根」已收盘 ADX 判定（与线上引擎
    #     `adx_arr[i-1] < adx_min` 同义、无前视）；拦截时 A、B 两支入口**全部作废**。---
    _adx_prev = met.get("adx_prev")
    adx_gate_block = bool(ADX_GATE > 0 and _adx_prev is not None
                          and float(_adx_prev) < ADX_GATE)
    if adx_gate_block:
        a_long = a_short = b_long = b_short = False
    met["adx_gate"] = float(ADX_GATE)
    met["adx_gate_block"] = adx_gate_block
    do_long = a_long or b_long
    do_short = a_short or b_short

    # 护栏：趋势衰竭抑制新开（|20日涨跌| > 15%）
    why_block = None
    if GUARD_EXHAUST:
        _p20 = met.get("p20", 0.0) or 0.0
        if abs(_p20) > EXT_TREND and (do_long or do_short):
            why_block = (f"护栏拦截：|20日涨跌| {abs(_p20)*100:.0f}% > {EXT_TREND*100:.0f}%，"
                         f"趋势衰竭，不追顶/抄底")
            do_long = do_short = False

    if do_long and do_short:      # 理论上互斥，保险起见取方向层一侧
        do_long = ok_long
        do_short = not ok_long

    if do_long:
        met["action"] = "做多"
        met["entry"] = close
        met["stop"] = close - SLK * atr_v
        met["target"] = None
        met["breakeven"] = close + BER * atr_v
        met["trail"] = TRK * atr_v
        _sig = "A 回踩重启" if a_long else "B 10根突破"
        met["reason"] = f"融合·方向层多头(EMA20上) + {_sig}｜止损2×ATR / 保本线{BER}×ATR"
        met["stage"] = "回踩启动" if a_long else "突破补位"
    elif do_short:
        met["action"] = "做空"
        met["entry"] = close
        met["stop"] = close + SLK * atr_v
        met["target"] = None
        met["breakeven"] = close - BER * atr_v
        met["trail"] = TRK * atr_v
        _sig = "A 回踩重启" if a_short else "B 10根突破"
        met["reason"] = f"融合·方向层空头(EMA20下) + {_sig}｜止损2×ATR / 保本线{BER}×ATR"
        met["stage"] = "回踩启动" if a_short else "突破补位"
    else:
        met["action"] = "观望"
        met["entry"] = None; met["stop"] = None; met["target"] = None
        met["breakeven"] = None
        met["trail"] = TRK * atr_v
        if why_block is not None:
            met["reason"] = why_block
        elif adx_gate_block:
            met["reason"] = (f"ADX={float(_adx_prev):.0f}<{ADX_GATE:.0f} 震荡市，扣扳机作废"
                             f"（融合硬门槛·用上一根ADX）")
        elif ok_long:
            met["reason"] = "融合·方向层多头(EMA20上)，但未满足 A/B 入场（等回踩重启或 10 根突破）"
        elif ok_short:
            met["reason"] = "融合·方向层空头(EMA20下)，但未满足 A/B 入场（等回踩重启或 10 根突破）"
        else:
            met["reason"] = "融合·方向层临界(EMA20走平)，震荡市跳过信号"
        met["stage"] = "观望"

    # 护栏(iii) — 简报层入场警示（不强制禁入；执行跳空由 backtest --gap-chase / 实盘手动判定）
    # 若当前收盘已远离 10 日突破点 K×ATR / 2%，在 reason 末尾追加"⚠️延伸警示·明日开盘警惕跳空"
    # 文案——让用户/回测执行层感知；但 action 保留可操作，由入场方按 open-vs-close 关系自行决定。
    # 实盘真正"追高陷阱"发生在次日开盘跳空：见 _fut_backtest.py 的 --gap-chase 机制。
    if GUARD_ENTRY_ZONE and met.get("action") in ("做多", "做空") and met.get("atr"):
        _atr = met["atr"]
        _close = met["close"]
        _anchor = met.get("ll10") if met["action"] == "做多" else met.get("hh10")
        if _anchor:
            _gap_pts = abs(_close - float(_anchor))
            _gap_atr = _gap_pts / _atr if _atr else 0.0
            _gap_pct = _gap_pts / float(_anchor) if _anchor else 0.0
            if _gap_atr > ENTRY_CHASE_ATR or _gap_pct > ENTRY_CHASE_PCT:
                _dir_act = met["action"]
                _kind_cn = "追高" if _dir_act == "做多" else "杀低"
                _ref_cn = "10日低点 ll10" if _dir_act == "做多" else "10日高点 hh10"
                # 注意：action 不变！只在 reason 末尾追加警示，前置展示会带上 ⚠️ 标记
                met["reason"] = (
                    f"{met.get('reason','')}｜⚠️{_kind_cn}警示：现价 {met['close']:,.0f} 距{_ref_cn} {float(_anchor):,.0f} "
                    f"已 {int(_gap_pts)} 点（{_gap_atr:.1f}×ATR / {_gap_pct*100:.1f}%），"
                    f"明日开盘警惕同向跳空、若开幅>0.5%建议放弃本次入场"
                )

    # 统一置信度（简报限仓排序 + 回测按置信度取前 N 共用）
    met["conviction"] = conviction_score(met)
    met["score"] = met["conviction"]
    return met


# ---------- 换月展期（末端锚定版，2026-09-06 实装）----------
# 判据与 _fut_backtest.py::back_adjust 保持同步（>max(12%×价, 8×近40日|gap|中位数)）。
ROLL_PCT = 0.12
ROLL_K = 8.0


def roll_adjust_end(series):
    """主连换月跳空展期 · 末端锚定：历史价位平移对齐，**当前主力时代价格保持真实不变**。

    原理：移仓时新旧主力方向几乎总是一致，合成跳空只是"价位水平差"（基差/贴升水），
    个人账户移仓不会瞬间盈亏该差值——主连拼接却把它当真实盈亏，污染 MA250/回测记账。
    展期 = 把历史整体平移对齐，模拟"移仓无瞬间盈亏"的现实。
    与回测版（起点锚定）的区别：简报展示/执行的当前价必须与行情软件一致，故锚定末端。
    判据保守（仅消除巨型跳空），漏检的小换月与普通波动同量级、无害。"""
    df = series.copy().reset_index(drop=True)
    n = len(df)
    if n < 60:
        return df
    c = df["close"].astype(float).to_numpy().copy()
    o = df["open"].astype(float).to_numpy().copy()
    h = df["high"].astype(float).to_numpy().copy()
    l = df["low"].astype(float).to_numpy().copy()
    gap = np.zeros(n); gap[1:] = o[1:] - c[:-1]
    mg = pd.Series(np.abs(gap)).rolling(40, min_periods=20).median().to_numpy()
    cum = 0.0
    for i in range(1, n):
        base = c[i - 1]
        if base > 0 and np.isfinite(gap[i]):
            th = max(ROLL_PCT * base, ROLL_K * (mg[i] if np.isfinite(mg[i]) else 0.0))
            if abs(gap[i]) > th:
                cum += gap[i]
        if cum != 0:
            c[i] -= cum; o[i] -= cum; h[i] -= cum; l[i] -= cum
    cum_final = cum
    if cum_final != 0:
        # 末端锚定：整体回移，使最后一个时代（当前主力）价格与真实行情逐位一致
        c += cum_final; o += cum_final; h += cum_final; l += cum_final
        if (not np.isfinite(c).all()) or (c <= 0).any() or (l <= 0).any():
            return series.copy().reset_index(drop=True)   # 极端结构防御：回退原始序列
    df["close"] = c; df["open"] = o; df["high"] = h; df["low"] = l
    return df


def analyze(series, root=None):
    """root 传入时一并完成引擎信号判定；不传则只算技术指标。
    指标计算基于展期序列（消除换月假跳空）；末端锚定保证当前价/点位与真实行情一致。"""
    df = roll_adjust_end(series).tail(300).reset_index(drop=True)
    c = df["close"]; close = float(c.iloc[-1])
    ma5, ma10, ma20, ma60 = [float(c.rolling(k).mean().iloc[-1]) for k in (5, 10, 20, 60)]
    ma120 = float(c.rolling(120).mean().iloc[-1]) if len(c) >= 120 else ma60
    ma250 = float(c.rolling(250).mean().iloc[-1]) if len(c) >= 250 else None
    adxv, pdi, mdi = adx(df)
    adx_v = float(adxv.iloc[-1]); pdi_v = float(pdi.iloc[-1]); mdi_v = float(mdi.iloc[-1])
    # V3.1 门控用「上一根」ADX（对齐线上引擎 adx_arr[i-1]）
    adx_prev_v = float(adxv.iloc[-2]) if len(adxv) >= 2 else None
    dpos = float(donchian_pos(df).iloc[-1])
    dif, dea, bar = macd(df); dif_v, dea_v, bar_v = float(dif.iloc[-1]), float(dea.iloc[-1]), float(bar.iloc[-1])
    a = float(atr(df).iloc[-1])
    p5 = float(c.iloc[-1]) / float(c.iloc[-6]) - 1 if len(c) >= 6 else 0.0
    p20 = float(c.iloc[-1]) / float(c.iloc[-21]) - 1 if len(c) >= 21 else 0.0
    hold = float(df["hold"].iloc[-1])
    date_v = str(df["date"].iloc[-1])[:10]
    # 10 日突破（用前 10 根高低点，不含当日）
    hh10 = float(df["high"].iloc[-11:-1].max()) if len(df) >= 11 else close
    ll10 = float(df["low"].iloc[-11:-1].min()) if len(df) >= 11 else close
    # 融合方向层：日线 EMA20（≈ 小时 EMA140）；用 REF 已收盘值
    ema20_s = ema(c, 20)
    ema20_v = float(ema20_s.iloc[-1]) if len(ema20_s) >= 1 else None
    ema20_prev_v = float(ema20_s.iloc[-2]) if len(ema20_s) >= 2 else None
    close_prev_v = float(c.iloc[-2]) if len(c) >= 2 else None
    # 融合入场层：快线 EMA10（≈ 小时 EMA20，用于 A 回踩重启）
    ema10_s = ema(c, 10)
    ema10_v = float(ema10_s.iloc[-1]) if len(ema10_s) >= 1 else None
    ema10_prev_v = float(ema10_s.iloc[-2]) if len(ema10_s) >= 2 else None
    o_last = float(df["open"].iloc[-1]); h_last = float(df["high"].iloc[-1]); l_last = float(df["low"].iloc[-1])
    # 吊灯参考：近 300 根极值收盘（供持仓台账推算 2×ATR 吊灯移动止损）
    pk_win = float(c.max()); tr_win = float(c.min())

    trend_up = (ma20 > ma60) and (close > ma60)
    trend_dn = (ma20 < ma60) and (close < ma60)
    if adx_v < ADX_THR:
        trend = "震荡"; stage = "震荡市"
    elif trend_up:
        trend = "多头"
        stage = "加速期" if dpos > 1.0 else ("启动期" if pdi_v > mdi_v else "成熟·衰退")
    elif trend_dn:
        trend = "空头"
        stage = "加速期" if dpos < 0 else ("启动期" if mdi_v > pdi_v else "成熟·衰退")
    else:
        trend = "震荡"; stage = "转折期(强ADX)"

    met = dict(root=root, date=date_v, close=close, open=o_last, high=h_last, low=l_last,
               ma5=ma5, ma20=ma20, ma60=ma60, ma120=ma120, ma250=ma250,
               ema20=ema20_v, ema20_prev=ema20_prev_v, ema10=ema10_v, ema10_prev=ema10_prev_v,
               close_prev=close_prev_v,
               adx=adx_v, adx_prev=adx_prev_v, pdi=pdi_v, mdi=mdi_v, dpos=dpos, dif=dif_v, dea=dea_v, bar=bar_v,
               atr=a, p5=p5, p20=p20, hold=hold, trend=trend, stage=stage,
               brk_up=close > hh10, brk_dn=close < ll10, hh10=hh10, ll10=ll10,
               pk_win=pk_win, tr_win=tr_win)

    # ---------- V3.4 门槛口径：斐波汇流（末根判定）+ 台账加仓所需序列 ----------
    # 方向按与 fusion_signal 完全相同的规则（close_prev vs ema20_prev）先行判定，
    # 以便对「A 回踩支路」做斐波汇流门控；B 突破支路不受影响。
    if ema20_prev_v is not None and close_prev_v is not None:
        _dir_v = ("多头" if close_prev_v > ema20_prev_v
                  else ("空头" if close_prev_v < ema20_prev_v else "震荡"))
    else:
        _dir_v = "震荡"
    if FIB_CONFL:
        _fib = fib_confluence(df, _dir_v, a, FIB_W, FIB_RATIOS, FIB_TOL_ATR)
        met["fib_ok"] = _fib["ok"]
        met["fib_pivot"] = _fib["pivot"]
        met["fib_levels"] = _fib["levels"]
        met["fib_hit"] = _fib["hit"]
        met["fib_leg_hi"] = _fib["leg_hi"]
        met["fib_leg_lo"] = _fib["leg_lo"]
    else:
        met["fib_ok"] = True
    met["fib_direction"] = _dir_v
    # 台账加仓「收盘创入场以来新高/新低」比对所需：内嵌调整后序列尾部（与信号同源，末端锚定）
    _tail = df.tail(ADDON_SEQ_BARS)
    met["seq_dates"] = [str(x)[:10] for x in _tail["date"].tolist()]
    met["seq_high"] = [float(x) for x in _tail["high"].tolist()]
    met["seq_low"] = [float(x) for x in _tail["low"].tolist()]
    return fusion_signal(met) if root else met


def fetch_series(sym, retries=3):
    if ak is None:
        raise RuntimeError("akshare 未安装，无法抓取行情；请先 pip install akshare 或改用已缓存数据")
    for attempt in range(retries):
        try:
            h = ak.futures_zh_daily_sina(symbol=sym)
            if h is not None and len(h) > 30:
                return h[["date", "open", "high", "low", "close", "volume", "hold"]]
        except Exception:
            pass
        try:
            # 具体合约（MA2701/CF2701 等）的回退：先尝试 sina 该合约（主连也走这条），失败时按 root + "0" 取主连
            root = "".join(ch for ch in sym if ch.isalpha())
            h = ak.futures_hist_em(symbol=(root + "0") if root else sym, period="daily",
                                   start_date="20230101", end_date="20261231")
            if h is not None and len(h) > 30:
                h = h.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                      "最低": "low", "收盘": "close", "成交量": "volume",
                                      "持仓量": "hold"})
                return h[["date", "open", "high", "low", "close", "volume", "hold"]]
        except Exception:
            pass
        time.sleep(2)
    return None


def fetch_series_contract(sym, root=None, retries=3):
    """M2 P1：抓取**具体合约（合约级）**日线。

    优先东方财富 futures_hist_em（标准 ROOTYYMM 全所通用、稳定），回退新浪
    futures_zh_daily_sina；均失败返回 None（调用方应回退主连以保证简报不空）。
    返回列：date/open/high/low/close/volume/hold（与 fetch_series 一致）。"""
    if ak is None:
        raise RuntimeError("akshare 未安装，无法抓取行情；请先 pip install akshare 或改用已缓存数据")
    for attempt in range(retries):
        try:
            h = ak.futures_hist_em(symbol=sym, period="daily",
                                   start_date="20230101", end_date="20261231")
            if h is not None and len(h) > 30:
                h = h.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                      "最低": "low", "收盘": "close", "成交量": "volume",
                                      "持仓量": "hold"})
                return h[["date", "open", "high", "low", "close", "volume", "hold"]]
        except Exception:
            pass
        try:
            h = ak.futures_zh_daily_sina(symbol=sym)
            if h is not None and len(h) > 30:
                return h[["date", "open", "high", "low", "close", "volume", "hold"]]
        except Exception:
            pass
        time.sleep(2)
    return None


# ---------- 昨日快照对比 ----------
def load_prev_snapshot(today):
    files = sorted(glob.glob(os.path.join(OUT_DIR, "_brief_actions_*.json")))
    for p in reversed(files):
        base = os.path.basename(p)
        d = base.replace("_brief_actions_", "").replace(".json", "")
        if d >= today.replace("-", ""):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f), d
        except Exception:
            continue
    return None, None


def save_snapshot(today, results):
    payload = dict(date=today, items={
        x["root"]: dict(action=x.get("action"), engine=x.get("engine"),
                        close=x.get("close"), stop=x.get("stop"), target=x.get("target"),
                        symbol=x.get("symbol"))   # M2 P1：记录活跃合约号，供换月判定
        for x in results})
    p = os.path.join(OUT_DIR, f"_brief_actions_{today.replace('-','')}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    return p


def diff_with_prev(results, prev):
    """返回 (新增, 反转, 消失) 三个列表，元素为 (root, name, 描述)"""
    new, flip, gone = [], [], []
    if not prev:
        return new, flip, gone
    prev_items = prev.get("items", {})
    cur = {x["root"]: x for x in results}
    for root, x in cur.items():
        p = prev_items.get(root)
        ca = x.get("action")
        if not p:
            if ca != "观望":
                new.append((root, x["name"], f"新信号 {ca}"))
            continue
        pa = p.get("action")
        if ca != "观望" and pa in (None, "观望"):
            new.append((root, x["name"], f"{pa or '无'} → {ca}"))
        elif ca != "观望" and pa != "观望" and ca != pa:
            flip.append((root, x["name"], f"{pa} → {ca}（方向反转，持仓需反手）"))
        elif ca == "观望" and pa not in (None, "观望"):
            gone.append((root, x["name"], f"{pa} → 观望（信号消失，注意减仓/离场）"))
    return new, flip, gone


# ---------- 主流程 ----------
def main():
    args = sys.argv[1:]
    force = "--force" in args
    render_flag = "--render" in args
    fund_flag = "--fund" in args
    if "--fetch-predictions" in args:
        refresh_predictions_best_effort()
        return
    univ = pd.read_csv(UNIV, encoding="utf-8-sig")
    today = datetime.date.today().strftime("%Y-%m-%d")
    jpath = os.path.join(PROC_DIR, f"_brief_tech_{today.replace('-','')}.json")

    if not force and os.path.exists(jpath):
        try:
            prev = json.load(open(jpath, encoding="utf-8"))
            _has_fusion = all(("ema20" in x and "ema10" in x) for x in prev.get("results", []))
            if len(prev.get("results", [])) == len(univ) and not prev.get("pending") and _has_fusion:
                print(f"[skip-fetch] 复用 {os.path.basename(jpath)}（{len(prev['results'])} 合约），跳过抓取")
                for x in prev["results"]:
                    fusion_signal(x)
                return _finalize(today, prev["results"], prev["pending"], univ, render_flag, jpath, fund_flag)
        except Exception:
            pass

    results, pending = [], []
    _n = len(univ)
    _asof = datetime.date.today()
    for _i, (_, r) in enumerate(univ.iterrows(), 1):
        root0 = r["root"]
        # M2 P1 + 2026-09-23 修复：解析当前活跃合约（合约级）。默认 live_heuristic
        # （新浪持仓量择优优先，heuristic 兜底）——RU2610 事故：纯 heuristic 近月规则
        # 把主力 1/5/9 的橡胶选到清淡 RU2610（价差 4.4%）。失败再退主连。
        C = active_contract(root0, _asof, strategy="live_heuristic")
        print(f"[{_i}/{_n}] 抓取 {root0} 活跃合约 {C} ...", flush=True)
        m = fetch_series_contract(C, root0)
        if m is None:
            # 兜底：退回主连，保证简报不空（标注降级）
            m = fetch_series(f"{root0}0")
            if m is not None:
                C = f"{root0}0"
        if m is None:
            pending.append(root0); continue
        try:
            met = analyze(m, root0)
            met["root"] = root0; met["name"] = r["name"]; met["exch"] = r["exch"]
            met["symbol"] = C; met["margin"] = r["margin"]
            results.append(met)
        except Exception as e:
            pending.append(f"{root0}({e})")
    print(f"成功分析: {len(results)}  待恢复: {len(pending)} -> {pending}")
    return _finalize(today, results, pending, univ, render_flag, jpath, fund_flag)


def _finalize(today, results, pending, univ, render_flag, jpath, fund_flag=False):
    def sort_key(x):
        # 可操作优先（做多→做空），其次震荡；组内按 score 降序
        order = {"做多": 0, "做空": 1, "观望": 2}
        return (order.get(x.get("action", "观望"), 2), -x.get("score", 0))
    results = sorted(results, key=sort_key)

    payload = dict(today=today, results=results, pending=pending, univ=univ.to_dict(orient="records"))
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    print("已导出技术指标JSON:", jpath)
    save_snapshot(today, results)

    # best-effort：刷新期货预测平台最新预测（写入 _predictions.json，供总览表说明列读取）
    refresh_predictions_best_effort()

    if render_flag:
        fundamentals = {}
        if fund_flag:
            # M2 P2：轻基本面 JSON 由 _fut_fund_light.py 写入 PROC_DIR（过程思考）；
            # 兼容旧路径 OUT_DIR（持仓情况和手续费）
            fname = f"_brief_fund_{today.replace('-','')}.json"
            fpath = os.path.join(PROC_DIR, fname)
            if not os.path.exists(fpath):
                fpath = os.path.join(OUT_DIR, fname)
            if os.path.exists(fpath):
                try:
                    with open(fpath, encoding="utf-8") as f:
                        fundamentals = json.load(f)
                except Exception:
                    pass
        md = render(results, pending, univ, fundamentals, today)
        out = os.path.join(BRIEF_DIR, f"国内期货多空策略简报_{today.replace('-','')}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        print("已生成简报:", out)
        return out
    return jpath


def fmt_px(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) else "-"


def lots_advice(x, cfg):
    """按 2% 风险倒推手数；超预算直接判 0（不做）。返回 (文案, 手数, 单手止损成本)"""
    root = x["root"]; sp = FUT_SPECS.get(root, {})
    mult = sp.get("multiplier", 1)
    stop_cost = x["atr"] * SLK * mult
    risk_amt = cfg["account"]["资金"] * cfg["account"]["单笔风险"]
    if stop_cost <= 0:
        return "-", 0, 0.0
    if stop_cost > risk_amt * 2:
        return "0（❌超预算不做）", 0, stop_cost
    if stop_cost > risk_amt:
        return "1 手 ⚠️略超", 1, stop_cost
    return f"{max(1, int(risk_amt / stop_cost))} 手", max(1, int(risk_amt / stop_cost)), stop_cost


def discipline_check_md(active_df, cfg, today, gate_on):
    """第一章「今日纪律检查」小节：基于持仓台账真实数据，固定输出三项，触发即 🔴。
    ① 单笔风险（买入点位 vs 止损位/参考止损 × 每手吨数 × 手数 > 账户2% 即超限）
    ② 同向加仓（同品种同方向 ≥2 腿，后开仓价劣于先开仓价 = 逆势摊平）
    ③ 持仓时长不对称（同品种浮亏腿持仓时长 > 浮盈腿持仓时长 = 重演截断盈利/放任亏损）
    另：趋势市门槛触发（gate_on）时明确写「今日不新开仓」。
    """
    from collections import defaultdict
    cap = cfg["account"]["资金"]
    risk_amt = cap * cfg["account"]["单笔风险"]          # 单笔风险上限（元）
    risk_pct = cfg["account"]["单笔风险"] * 100
    L = []
    L.append("### 今日纪律检查（基于持仓台账真实数据）\n")
    any_red = False

    # 封印新开仓提示
    if gate_on:
        L.append(f"- 🔴 **今日不新开仓**：趋势市门槛触发（全样本趋势占比 < 门槛），仅跟踪已有持仓，禁止手痒补单。")
        any_red = True
    else:
        L.append(f"- ✅ 今日可正常新开仓（趋势市门槛未触发）。")

    if len(active_df) == 0:
        L.append(f"- ✅ 当前无任何未平仓腿，三项检查均不适用（单笔风险 / 同向加仓 / 持仓时长不对称 均无标的）。")
        return "\n".join(L), any_red

    # ---------- ① 单笔风险 ----------
    risk_red = []
    risk_unknown = 0
    lines_risk = []
    for _, r in active_df.iterrows():
        sl = POS._num(r.get("止损位")) or POS._num(r.get("参考止损"))
        entry = POS._num(r.get("买入点位"))
        mult = POS._num(r.get("每手吨数")) or FUT_SPECS.get(POS.norm_root(str(r["代码"]).strip()), {}).get("multiplier", 1)
        lots = POS._num(r.get("手数")) or 0
        if entry is None or not mult or not lots:
            continue
        if sl is None:
            # 缺止损位/参考止损：无法核算单笔风险，本身是纪律风险，明确提示
            lines_risk.append(
                f"- ⚠️ **单笔风险·{r['品种']}{r['代码']}（{r['方向']}{int(lots)}手@{fmt_px(entry)}）**："
                f"缺止损位/参考止损，无法核算单笔风险，**请立即补止损位**"
            )
            any_red = True
            risk_unknown += 1
            continue
        per_risk = abs(entry - sl) * mult * lots
        over = per_risk > risk_amt
        flag = "🔴" if over else "✅"
        if over:
            risk_red.append(f"{r['品种']}{r['代码']}")
            any_red = True
        lines_risk.append(
            f"- {flag} **单笔风险·{r['品种']}{r['代码']}（{r['方向']}{int(lots)}手@{fmt_px(entry)}）**："
            f"止损 {fmt_px(sl)} → 单笔风险 **{per_risk:,.0f} 元**（账户 {per_risk/cap*100:.1f}%，"
            f"{'超限' if over else '合规'} ≤{risk_pct:.0f}%）"
        )
    L.append(f"**① 单笔风险（上限 {risk_amt:,.0f} 元 / {risk_pct:.0f}%）**：")
    if lines_risk:
        L.extend(lines_risk)
    if risk_red:
        L.append(f"- 🔴 共 **{len(risk_red)}** 条腿单笔风险超限：{'、'.join(risk_red)}，须下调手数或收紧止损。")
    elif risk_unknown:
        L.append(f"- ⚠️ 有 **{risk_unknown}** 条腿缺止损位/参考止损无法核算单笔风险（已提示请补止损）；"
                 f"其余已核算腿均 ≤ {risk_amt:,.0f} 元（{risk_pct:.0f}%），无超限。")
    else:
        L.append(f"- ✅ 所有未平仓腿单笔风险均 ≤ {risk_amt:,.0f} 元（{risk_pct:.0f}%），无超限。")

    # ---------- ② 同向加仓 / 逆势摊平 ----------
    groups = defaultdict(list)
    for _, r in active_df.iterrows():
        groups[str(r["品种"]).strip()].append(r)
    L.append("**② 同向加仓（同品种同方向 ≥2 腿，检查是否逆势摊平）**：")
    found = False
    for root_name in sorted(groups.keys()):
        legs = groups[root_name]
        by_dir = defaultdict(list)
        for r in legs:
            by_dir[str(r["方向"]).strip()].append(r)
        for direction, dl in by_dir.items():
            if len(dl) < 2:
                continue
            found = True
            dl_sorted = sorted(dl, key=lambda x: str(x.get("买入日期", "")))
            pe = POS._num(dl_sorted[0].get("买入点位"))
            ce = POS._num(dl_sorted[-1].get("买入点位"))
            if pe is None or ce is None:
                continue
            if direction == "做多":
                worse = ce < pe          # 越买越低 = 摊平
                normal_txt = "越买越高"
            else:  # 做空
                worse = ce > pe          # 越卖越高 = 摊平
                normal_txt = "越卖越低"
            if worse:
                L.append(f"- 🔴 **{root_name}（{direction}）×{len(dl)}腿 逆势摊平**：后开仓 {fmt_px(ce)} 劣于先开仓 {fmt_px(pe)}"
                         f"（{direction}应{normal_txt}才是顺趋势加仓）→ ⚠️ 逆势摊平，须止损而非加码")
                any_red = True
            else:
                L.append(f"- ✅ {root_name}（{direction}）×{len(dl)}腿：后开仓 {fmt_px(ce)} 优于先开仓 {fmt_px(pe)}（{normal_txt}，金字塔加仓正常）")
    if not found:
        L.append("- ✅ 无同品种同方向 ≥2 条未平仓腿，不存在同向加仓/摊平。")

    # ---------- ③ 持仓时长不对称 ----------
    today_ts = pd.Timestamp(today)
    def _dur(r):
        bd = pd.to_datetime(str(r.get("买入日期", "")).strip(), errors="coerce")
        return (today_ts - bd).days if pd.notna(bd) else None
    L.append("**③ 持仓时长不对称（同品种浮亏腿持仓时长 > 浮盈腿 = 重演截断盈利/放任亏损）**：")
    asym = []
    for root_name, legs in groups.items():
        loss_durs = [d for d in (_dur(r) for r in legs if (POS._num(r.get("净浮盈")) or 0) < 0) if d is not None]
        profit_durs = [d for d in (_dur(r) for r in legs if (POS._num(r.get("净浮盈")) or 0) > 0) if d is not None]
        if loss_durs and profit_durs:
            max_loss = max(loss_durs); min_profit = min(profit_durs)
            if max_loss > min_profit:
                asym.append((root_name, max_loss, min_profit))
    if asym:
        for root_name, ld, pd_ in asym:
            L.append(f"- 🔴 **{root_name}**：浮亏腿持仓 {ld} 天 > 浮盈腿 {pd_} 天 → 正在重演「截断盈利、放任亏损」")
            any_red = True
        L.append("- 🔴 请核对：盈利腿是否过早兑现、亏损腿是否在死扛；按纪律执行止盈止损。")
    else:
        L.append("- ✅ 当前持仓无「浮亏腿持仓时长 > 浮盈腿」的情形。")

    return "\n".join(L), any_red


def load_predictions():
    """读取期货预测平台最新预测（本地缓存 JSON，键=品种根，如 SA/MA/EG）。"""
    p = os.path.join(PROC_DIR, "_predictions.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _pred_of(preds, root):
    """取某品种的预测详情文本；无则返回占位符。"""
    d = preds.get(root) or preds.get(root.upper()) or {}
    t = d.get("text") or d.get("prediction") or ""
    return t.strip() if t else "—"


def _fund_note(fund_map, root):
    """M2 P2：把轻基本面三分项压成总览表『说明』列的紧凑文案（无数据返回空串）。"""
    d = (fund_map or {}).get(root) or {}
    if not d:
        return ""
    seg = []
    b = d.get("basis") or {}
    if b.get("chg") is not None:
        seg.append(f"基差{b['chg']*100:+.1f}pct")
    p = d.get("position") or {}
    if p.get("chg") is not None:
        seg.append(f"主力{p['chg']:+,.0f}")
    iv = d.get("inventory") or {}
    if iv.get("chg_ratio") is not None:
        seg.append(f"库存{iv['chg_ratio']*100:+.0f}%")
    sc = d.get("score")
    note = "·".join(seg)
    if sc is not None:
        note += f"｜基本面{sc:+.1f}"
    return note


def _fmt_pred(name, p):
    """把平台 prediction 对象格式化为简报表格『说明』列文案。"""
    d = p.get("direction")
    if not d:
        return ""
    dir_cn = {"up": "偏多", "down": "偏空"}.get(d, d)
    prob = p.get("direction_prob")
    if prob is None:
        return ""
    parts = [f"{dir_cn} 概率{prob*100:.1f}%"]
    rp = p.get("ret_point")
    if rp is not None:
        parts.append(f"幅度{rp:+.2f}%")
    conf = p.get("confidence")
    if conf is not None:
        parts.append(f"置信{conf*100:.1f}%")
        if conf < 0.1:
            parts.append("·低置信")
    return (name + "预测 " if name else "") + " ".join(parts)


def fetch_predictions(base_url=None, timeout=8):
    """从期货预测平台（qhyc）拉取全品种最新预测，写入 _predictions.json（键=品种根）。
    返回写入条数；任何异常向上抛出，由调用方 best-effort 捕获。"""
    import urllib.request, json as _json, urllib.error
    base_url = (base_url or os.environ.get("QH_PRED_URL", "http://127.0.0.1:8000")).rstrip("/")
    url = base_url + "/dashboard/overview"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    syms = data.get("symbols", []) if isinstance(data, dict) else data
    preds = {}
    for s in syms:
        sym = s.get("symbol", "")
        root = sym[:-3] if sym.endswith("888") else sym
        p = s.get("prediction")
        if not p:
            continue
        txt = _fmt_pred(s.get("name", ""), p)
        if not txt:
            continue
        preds[root] = {
            "text": txt,
            "updated": p.get("target_date") or p.get("as_of_date"),
            "raw": p,
        }
    out = os.path.join(PROC_DIR, "_predictions.json")
    with open(out, "w", encoding="utf-8") as f:
        _json.dump(preds, f, ensure_ascii=False, indent=2)
    return len(preds)


def refresh_predictions_best_effort(base_url=None, timeout=8):
    """best-effort：拉取失败不影响简报生成，沿用已有缓存。"""
    try:
        n = fetch_predictions(base_url, timeout)
        print(f"[predictions] 已从期货预测平台刷新 {n} 个品种预测 -> "
              f"{os.path.join(PROC_DIR, '_predictions.json')}")
        return n
    except Exception as e:
        print(f"[predictions] 警告：拉取期货预测平台失败（{e}），沿用已有 _predictions.json"
              f"（若为空则总览表说明列显示『—』）", file=sys.stderr)
        return 0


# ---------- 渲染 ----------
def render(results, pending, univ, fundamentals=None, today=None):
    today = today or datetime.date.today().strftime("%Y-%m-%d")
    # M2 P2：轻基本面。未由 --fund 传入时自动尝试读取当日 JSON（_fut_fund_light.py 产出）
    if not fundamentals:
        try:
            from _fut_fund_light import load_fund
            fundamentals = load_fund(today) or {}
        except Exception:
            fundamentals = {}
    fundamentals = fundamentals or {}
    fund_map = fundamentals.get("by_root", fundamentals) if isinstance(fundamentals, dict) else {}
    fund_macro = fundamentals.get("macro_regime", "") if isinstance(fundamentals, dict) else ""
    # M3：研究层（多空辩论 × 量化验证）；由 _fut_research.py 产出
    research = None
    try:
        from _fut_research import load_research
        research = load_research(today)
    except Exception:
        research = None
    res_map = (research or {}).get("by_root", {}) if isinstance(research, dict) else {}
    # M4：基本面/宏观/情绪 Agent（含板块宏观传导、资金情绪代理、完备度置信）
    agent = None
    try:
        from _fut_fundamentals_agent import load_agent
        agent = load_agent(today)
    except Exception:
        agent = None
    agent_map = (agent or {}).get("by_root", {}) if isinstance(agent, dict) else {}
    cfg = POS.load_fee_config()
    preds = load_predictions()                       # 期货预测平台最新预测（本地缓存）
    pos_df, pos_sum = POS.refresh_positions(results, cfg, FUT_SPECS, today)
    # 活跃持仓（排除已平仓行）：用于额度/映射；已平仓行仍在第四章展示历史（Bug1 修复）
    closed_mask = pos_df.apply(POS.is_closed, axis=1) if len(pos_df) else pd.Series([], dtype=bool)
    active_df = pos_df[~closed_mask] if len(pos_df) else pos_df
    pos_map = {}
    if len(active_df):
        for _, r in active_df.iterrows():
            pos_map[POS.norm_root(str(r["代码"]).strip())] = r
            pos_map[str(r["品种"]).strip()] = r  # 品种根兜底（兼容代码仅填数字后缀的情形）
    # 统一按可操作性重排（做多 → 做空，组内按信号强度降序；观望不展示）
    results = sorted(results, key=lambda x: ({"做多": 0, "做空": 1, "观望": 2}.get(x.get("action", "观望"), 2),
                                             -x.get("score", 0)))
    # 用户偏好：总览表只展示方向性信号，震荡无建议的"观望"不占展示位
    display_results = [x for x in results if x.get("action") in ("做多", "做空")]
    # 融合方向层（EMA20）与中期 MA20/60 趋势可能背离（长趋势中的回调），
    # 统一给出提示，避免"趋势列显示空头、却建议做多"的困惑
    for x in results:
        if x.get("action") != "观望":
            _dir = x.get("direction")
            if (_dir == "多头" and x.get("trend") == "空头") or (_dir == "空头" and x.get("trend") == "多头"):
                x["reason"] = x.get("reason", "") + "｜⚠️中期 MA20/60 与融合方向层(EMA20)背离（长趋势中的回调）"
    prev, prev_date = load_prev_snapshot(today)
    news, flips, gones = diff_with_prev(results, prev)
    # Bug2 修复：记录昨日各品种动作，用于判定「今日信号是否为昨日已提示的同方向信号」，
    # 若是则不再按今日收盘价重复催买/催卖，改标「⏳已提示待执行」并沿用首日信号价。
    prev_items = prev.get("items", {}) if prev else {}
    def was_alerted(root, act):
        p = prev_items.get(root)
        return p is not None and p.get("action") == act

    L = []
    L.append(f"# QW 分析 · 国内期货多空策略简报（{len(univ)} 品种 · 合约级活跃合约）\n")
    L.append("- **扫描日期**：" + today + "（收盘后，日线已确认）")
    L.append("- **分析框架**：Qi Analisy 融合策略 V3.4（额外执行口径）· 日线口径"
             "（方向层 EMA20 ≈ 小时 EMA140；门控 ADX≥15 + 斐波汇流；入场 A 回踩重启 / B 10根突破；"
             "止损 2×ATR 初始 + 2×ATR 吊灯 + 0.5R 保本；收盘价确认执行）")
    L.append("- **数据说明**：**合约级活跃合约**日 K（akshare/新浪/东方财富，截至 " + today +
             " 收盘）；五大商品交易所，剔除中金所股指期货；输入集经三层过滤：持仓量>10万手、保证金≤2万元/手")
    L.append(f"- **账户口径**：资金 ¥****（仅内部追踪）；单笔风险 ≤{cfg['account']['单笔风险']*100:.0f}%"
             f"、月亏 ≤{cfg['account']['月亏上限']*100:.0f}%（触及即本月停止交易）")
    L.append("- **执行口径（重要）**：**所有点位以当日收盘价界定，次日以开盘价执行**。"
             "总览表「参考买点」= 当日收盘价；止损 = 买点 ∓ 2×ATR（初始）；"
             "止盈 = **2×ATR 吊灯移动止损（无固止盈）**；保本线 = 买点 ± 0.5×ATR。"
             f"v2 时间止损：持仓满 **{MAX_HOLD_BARS}** 根 K 线仍未离场——**仍有浮盈** → 止损收紧至**保本价**；"
             "**已转亏/未盈利** → **次日开盘离场**（与回测 --time-stop 30 双分支一致）。"
             f"v2.1 锁仓：持仓浮亏 ≥ **{LOCK_TRIG}×ATR** 且信号未反转 → 次日开盘**反向等量锁仓**（并发 ≤{LOCK_MAX}）；"
             "锁仓后原仓止损/止盈冻结，**组合回补保本 → 平锁仓腿解锁**，信号反转/转弱 → **双平**。"
             "持仓台账与手续费表可由你直接编辑，脚本只刷新行情派生列；锁仓执行后回填「锁仓腿价」。\n")
    # ---- 仓位纪律：最多同时持仓 MAX_POS 个；按「反追高优先级」取前 N 入选执行 ----
    # 优先级 = 置信度 − 延伸惩罚（价格偏离慢均线越远=趋势越成熟/越易反转→越不优先）。
    # 与回测 --select priority 完全一致：避免集中追高最延伸的热门品种（原「稳定分散子集」
    # 在限仓 4 时退化为准随机选 4 个）。置信度分仍用于总览/章节排序展示。
    held_cnt = len(active_df) if len(active_df) else 0
    held_roots = set(POS.norm_root(str(r["代码"]).strip()) for _, r in active_df.iterrows())
    held_roots |= {str(r["品种"]).strip() for _, r in active_df.iterrows()}
    held_roots.discard("")
    # 融合引擎统一纳入可操作池（V1/V3/V4 派发已退役）
    actionable_all = [x for x in results if x.get("action") in ("做多", "做空")]
    new_cands = [x for x in actionable_all if x["root"] not in held_roots]
    def _brief_prio(x):
        # 反追高优先级（与回测 --select priority 同一公式）：置信度 − 延伸惩罚
        ma = x.get("ma250") or x.get("ma120")
        close = x.get("close") or 0.0
        ext = (close - float(ma)) / float(ma) if ma else 0.0
        if x.get("action") == "做空":
            ext = -ext
        pen = min(max(0.0, ext), 0.30) * 100.0   # 30% 偏离封顶扣 30 分
        return (-((x.get("score") or 0.0) - pen), x["root"])
    new_cands.sort(key=_brief_prio)   # 反追高优先级（与回测 --select priority 一致）
    slots = max(0, MAX_POS - held_cnt)
    # 趋势市门槛（与回测 --gate 一致）：全样本处趋势(多/空)占比低于阈值则封印新开仓；
    # 已有持仓仍按止盈/止损/反手/离场推进，仅不在本简报新开。
    _tn = len(results); _tt = sum(1 for x in results if x.get("trend") in ("多头", "空头"))
    trend_frac = (_tt / _tn) if _tn else 0.0
    gate_on = trend_frac < GATE
    # v2 板块限额（与回测 --sector-cap 同语义）：按优先级遍历候选，同板块最多 SECTOR_CAP 个新开，
    # 只统计本次新选入（不计已持仓——与回测非轮动模式一致），总仓不超过 MAX_POS。
    _sec_blocked = []
    if gate_on:
        selected_roots = set()   # 封印新开仓
    else:
        _sel = []
        _sec_cnt = {}
        for x in new_cands:
            if len(_sel) >= slots:
                break
            if SECTOR_CAP > 0:
                _sec = SECTOR.get(x["root"], "其他")
                if _sec_cnt.get(_sec, 0) >= SECTOR_CAP:
                    _sec_blocked.append(x["root"])
                    continue
            _sel.append(x["root"])
            if SECTOR_CAP > 0:
                _sec_cnt[_sec] = _sec_cnt.get(_sec, 0) + 1
        selected_roots = set(_sel)
    if gate_on:
        _gate_note = "⚠️ **趋势市门槛触发**：今日**封印新开仓**，仅跟踪已有持仓。"
    else:
        _gate_note = "趋势市门槛未触发，正常按优先级入选执行。"
    L.append(f"- **仓位纪律**：最多同时持仓 **{MAX_POS}** 个；今日**融合引擎**可操作 **{len(actionable_all)}** 个、"
             f"已持仓 **{held_cnt}** 个 → 剩余额度 **{slots}** 个；按**反追高优先级**取前 **{len(selected_roots)}** 个"
             f"**入选执行**，其余 **{len(new_cands)-len(selected_roots)}** 个标「观察」不新开仓"
             f"（优先级=置信度−延伸惩罚，避免追高最延伸的趋势）。"
             + (f"板块限额：同板块新开 ≤**{SECTOR_CAP}** 个"
                + (f"，今日因板块限额让位 {len(_sec_blocked)} 个（{'、'.join(_sec_blocked[:6])}"
                   f"{'…' if len(_sec_blocked) > 6 else ''}）" if _sec_blocked else "，今日未触发") + "。\n"
               if SECTOR_CAP > 0 else "\n"))
    L.append(f"- {_gate_note}\n")
    # ---- M2 P1：换月提示（对比上一运行记录的活跃合约号）----
    _rollover = []
    for x in results:
        _ps = (prev_items.get(x["root"], {}) or {}).get("symbol")
        _cs = x.get("symbol")
        _rn = rollover_note(x["root"], _ps, _cs, x.get("name", ""))
        if _rn:
            _rollover.append(_rn)
    if _rollover:
        L.append("- 🔄 **换月提示**（活跃合约已切换，旧仓注意次日开盘平旧开新）：")
        for _rr in _rollover:
            L.append(f"  - {_rr}")
        L.append("")
    L.append("---\n")

    # 信号分组（供二/三章与本期主线复用）
    longs = [x for x in results if x.get("action") == "做多"]
    shorts = [x for x in results if x.get("action") == "做空"]
    lm = longs           # 融合引擎统一分组（V1/V3/V4 派发退役）
    sm = shorts
    lv = []
    sv = []

    # ================= 一、总览表 =================
    L.append("## 一、总览表（按可操作性排序：做多 → 做空，震荡无建议不展示）\n")
    L.append("| # | 品种 | 收盘 | 5日% | 20日% | 趋势 | 阶段 | 引擎 | 建议操作 | 参考买点 | 止损 | 止盈 | 盈亏比 | 说明(期货预测平台) |")
    L.append("|---|------|------|------|-------|------|------|------|----------|----------|------|------|--------|------|")
    if display_results:
        for i, x in enumerate(display_results, 1):
            p5 = f"{x['p5']*100:+.1f}%"; p20 = f"{x['p20']*100:+.1f}%"
            eng = x.get("engine", FUSION_ENGINE)
            act = x.get("action", "观望")
            root = x["root"]
            disp_sym = x.get("symbol") or root     # M2 P1：显示活跃合约号（如 FG2601）而非主连 FG0
            entry = fmt_px(x.get("entry")); stop = fmt_px(x.get("stop"))
            targ = fmt_px(x.get("target")) if x.get("target") is not None else ("吊灯2×ATR" if act != "观望" else "-")
            rr = "吊灯" if act != "观望" else "-"
            # 趋势列 = 融合方向层（EMA20，只做顺势一侧）；震荡仅出现在方向层临界
            _dir_v = x.get("direction") or x.get("trend", "震荡")
            tr = f"**{_dir_v}**" if _dir_v != "震荡" else "震荡"
            reason = x.get("reason", "")

            pr = pos_map.get(root)
            if pr is not None:
                # ---- 已持仓：操作以持仓状态为准（点位取用户填写值，留空则用引擎参考值）----
                mark = "🔒"
                p_stop = fmt_px(POS._num(pr.get("止损位")) or POS._num(pr.get("参考止损")))
                p_targ = fmt_px(POS._num(pr.get("止盈位")) or POS._num(pr.get("参考止盈")))
                p_price = fmt_px(POS._num(pr.get("当前价")))
                p_act = str(pr.get("建议动作", ""))
                if p_act.startswith("✅"):
                    op = f"**✅触及止盈·平仓 @{p_targ}**"
                elif p_act.startswith("⛔"):
                    op = f"**⛔触及止损·平仓 @{p_stop}**"
                elif p_act.startswith("🔄"):
                    op = f"**🔄反手 @{p_price}（先平后开）**"
                elif p_act.startswith("⚠️趋势"):
                    op = f"**⚠️减仓/离场 @{p_price}**"
                elif p_act.startswith(("🔐", "🔒🔄", "🔒⚠️", "🔒✅", "🔒🔒")):
                    # v2.1 锁仓动作原样透传（含组合浮盈口径），不得折叠成「🔒持有」丢失解锁/双平指令
                    op = f"**{p_act}**"
                elif p_act.startswith("🔒"):
                    op = f"**🔒持有**（止损 {p_stop} / 止盈 {p_targ}）"
                else:
                    op = f"**{p_act}**"
                reason = f"持仓：{pr.get('方向','')} {pr.get('手数','')}手 @ {fmt_px(POS._num(pr.get('买入点位')))}｜{reason}"
                # 已持仓品种：买点/止损/止盈三列改按持仓口径显示，避免与新开仓点位混淆
                entry, stop, targ = "已持仓", p_stop, p_targ
            else:
                mark = ""
                if act in ("做多", "做空") and was_alerted(root, act):
                    # 昨日已提示同方向信号且今日仍未持仓 → 不重复催买/催卖，沿用首日信号价
                    op = "**⏳已提示待执行**"
                    op += " · ✅入选" if root in selected_roots else " · 👁观察"
                    pe = prev_items.get(root, {}).get("close")  # 首日信号价（融合中 entry==close）
                    if pe is not None:
                        entry = fmt_px(pe)
                    # 止损统一按融合 2×ATR 口径，以首日信号价为基准（用当日 ATR 近似）；
                    # 旧快照若为退役引擎（V1/V3/V4）的 1×ATR/固定目标值一律弃用，避免口径混用
                    _p_atr = x.get("atr") or 0
                    if pe is not None and _p_atr:
                        stop = fmt_px(float(pe) + SLK * _p_atr if act == "做空" else float(pe) - SLK * _p_atr)
                    else:
                        stop = fmt_px(x.get("stop"))
                    targ = "吊灯2×ATR"
                elif act == "做多":
                    op = "**买入做多**"
                elif act == "做空":
                    op = "**卖出做空**"
                else:
                    op = "观望"
                if act != "观望" and not was_alerted(root, act):
                    op += " · ✅入选" if root in selected_roots else " · 👁观察"
            # M2 P2：说明列 = 平台预测 + 轻基本面要点
            _pred_txt = _pred_of(preds, root)
            _fund_txt = _fund_note(fund_map, root)
            if _fund_txt:
                _base = "" if (_pred_txt in ("", "—")) else _pred_txt
                _note_cell = (_base + "｜" + _fund_txt) if _base else _fund_txt
            else:
                _note_cell = _pred_txt
            L.append(f"| {i} | {mark}{x['name'].replace('连续','')} {disp_sym} | {x['close']:.0f} | {p5} | {p20} | "
                     f"{tr} | {x['stage']} | {eng} | {op} | {entry} | {stop} | {targ} | {rr} | {_note_cell} |")
    elif results:
        # 数据已取到（≥1 个品种完成分析），但无一触发融合入场 → 是「无信号」，不是「接口故障」
        L.append(f"| - | （本期无品种触发融合入场条件：{len(results)} 个品种已分析，"
                 f"最高阶段仍为「观望」，按规则不新开仓——观望是规则不是懒惰）| | | | | | | | | | | | |")
    else:
        L.append("| - | （行情接口暂不可用，技术指标待恢复后补算）| | | | | | | | | | | | |")
    for p in pending:
        L.append(f"| - | ⏳ {p} | - | - | - | - | - | - | 待补算 | - | - | - | - | 行情接口未取到 |")
    L.append("")


    # 较昨日变化
    L.append("### 📌 较上一交易日变化\n")
    if prev:
        if news or flips or gones:
            for r, n, d in news:
                L.append(f"- 🆕 **{n} {r}**：{d}（新出现可操作信号）")
            for r, n, d in flips:
                L.append(f"- 🔄 **{n} {r}**：{d}")
            for r, n, d in gones:
                L.append(f"- ➖ **{n} {r}**：{d}")
        else:
            L.append("- 与上一交易日（" + prev_date + "）相比，可操作信号无变化。")
    else:
        L.append("- 未找到上一交易日的信号快照，本期为基线（次日开始自动对比）。")
    L.append("")

    # ================= 1.6 第一章 · 今日纪律检查（每期必查，任务规定 3 项） =================
    # 基于持仓台账真实数据，固定输出单笔风险 / 同向加仓 / 持仓时长不对称 三项，触发即 🔴。
    _disc_md, _disc_red = discipline_check_md(active_df, cfg, today, gate_on)
    L.append(_disc_md)
    L.append("")

    # ================= 二、可操作多头 =================
    sec = 2
    CN = {2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七"}
    L.append(f"## {CN[sec]}、可操作多头（融合引擎，点位以当日收盘价界定）\n")
    sec += 1
    L.append("| 品种 | 引擎 | 买点(收盘) | 止损 | 止盈 | 跟踪止损 | ATR | 手数建议 | 单笔成本 | 说明 |")
    L.append("|------|------|-----------|------|------|----------|-----|----------|----------|------|")
    lm = longs
    if lm:
        for x in lm:
            eng = x["engine"]; root = x["root"]
            lots_txt, lots, stop_cost = lots_advice(x, cfg)
            note = "⏳已提示待执行·沿用首日信号价" if was_alerted(root, "做多") else ""
            tail = (f"{x['reason']}（{'✅入选' if x['root'] in selected_roots else '👁观察'}"
                    f"{('｜' + note) if note else ''}）")
            _targ = fmt_px(x['target']) if x.get('target') is not None else "吊灯2×ATR"
            L.append(f"| {x['name'].replace('连续','')} {x.get('symbol') or root} | {eng} | {fmt_px(x['entry'])} | {fmt_px(x['stop'])} | "
                     f"{_targ} | 自最高点回撤 {x['trail']:.0f} 点 | {x['atr']:.0f} | {lots_txt} | "
                     f"{stop_cost*lots:,.0f} 元 |  |")
    else:
        L.append("| （本期无满足融合入场条件的多头信号）| | | | | | | | | |")
    L.append("")
    if lm:
        L.append("**执行提示**：以上点位为**当日收盘价口径**，次日以开盘价买入；买入后止损/止盈**仅在收盘价确认后**才动作，不盘中抢跑。\n")
    # （原「V1 引擎多头信号」子区已随 V1/V3/V4 引擎退役移除；融合引擎统一进主表）

    # ================= 三、可操作空头 =================
    L.append(f"## {CN[sec]}、可操作空头（融合引擎，点位以当日收盘价界定）\n")
    sec += 1
    L.append("| 品种 | 引擎 | 卖点(收盘) | 止损 | 止盈 | 跟踪止损 | ATR | 手数建议 | 单笔成本 | 说明 |")
    L.append("|------|------|-----------|------|------|----------|-----|----------|----------|------|")
    sm = shorts
    if sm:
        for x in sm:
            eng = x["engine"]; root = x["root"]
            lots_txt, lots, stop_cost = lots_advice(x, cfg)
            note = "⏳已提示待执行·沿用首日信号价" if was_alerted(root, "做空") else ""
            tail = (f"{x['reason']}（{'✅入选' if x['root'] in selected_roots else '👁观察'}"
                    f"{('｜' + note) if note else ''}）")
            _targ = fmt_px(x['target']) if x.get('target') is not None else "吊灯2×ATR"
            L.append(f"| {x['name'].replace('连续','')} {x.get('symbol') or root} | {eng} | {fmt_px(x['entry'])} | {fmt_px(x['stop'])} | "
                     f"{_targ} | 自最低点回撤 {x['trail']:.0f} 点 | {x['atr']:.0f} | {lots_txt} | "
                     f"{stop_cost*lots:,.0f} 元 |  |")
    else:
        L.append("| （本期无满足融合入场条件的空头信号）| | | | | | | | | |")
    L.append("")
    # （原「V1 引擎空头信号」子区已随 V1/V3/V4 引擎退役移除；融合引擎统一进主表）

    # ================= 四、持仓品种记录（仅当前持仓） =================
    L.append(f"## {CN[sec]}、持仓品种记录（可编辑 · 以你的记录为准）\n")
    sec += 1
    L.append("> 台账文件：`E:\\QH\\期货简报\\持仓情况和手续费\\_positions.csv`（Excel 可直接打开编辑）。"
             "**你填写的列（买入点位/手数/止损位/止盈位/手续费/备注）脚本只读不动；"
             "行情派生列（当前价/浮盈/建议动作）每次运行自动刷新。**\n")
    if len(active_df):
        L.extend(POS.positions_md_table(active_df))
        L.append("")
        # M2 P3：加仓观察子表（金字塔·盈利顺势仓）
        _addons = (pos_sum or {}).get("addons") or []
        if _addons:
            L.extend(POS.addon_md_table(_addons))
            L.append("")
    else:
        L.append("- 当前无未平仓持仓记录。若有实盘持仓，请在 `_positions.csv` 登记后重跑，系统会自动跟踪止盈止损。\n")

    # ================= 五、轻基本面边际（M2 P2） =================
    if fund_map:
        L.append(f"## {CN[sec]}、轻基本面边际（现货基差 / 主力资金 / 仓单库存）\n")
        sec += 1
        L.append(f"> 宏观：{fund_macro}" if fund_macro else "> 宏观：—（宏观数据缺失）")
        L.append("> 口径：基差 = 主力合约期货价 − 现货价（取 `dom_basis_rate` 变化，下行=贴水加深=现货偏紧→偏多）；"
                 "主力资金 = 主力合约前 5 会员净持仓变化；库存 = 交易所仓单/库存较上一期变化。"
                 "**诚实边界：不含社会库存/开工率/产量/进出口/上下游利润（付费墙），也不含新闻政策情绪（非结构化）；"
                 "无免费持仓排名源的品种标 N/A。**\n")
        # 展示范围：本期可操作信号 + 当前持仓
        _sym_by_root = {x["root"]: x.get("symbol") for x in results}
        _froots = []
        for x in display_results:
            if x["root"] in fund_map and x["root"] not in _froots:
                _froots.append(x["root"])
        if len(active_df):
            for _, r in active_df.iterrows():
                rr0 = POS.norm_root(str(r["代码"]).strip())
                if rr0 in fund_map and rr0 not in _froots:
                    _froots.append(rr0)
        _froots.sort(key=lambda r: -abs((fund_map.get(r) or {}).get("score") or 0))
        if _froots:
            L.append("| 品种 | 活跃合约 | 基本面评分 | 置信 | 基差率变化 | 前5会员净持仓变化 | 库存变化 | 缺口标注 |")
            L.append("|------|----------|------------|------|------------|-------------------|----------|----------|")
            for r in _froots[:15]:
                d = fund_map.get(r) or {}
                b, p, iv = d.get("basis") or {}, d.get("position") or {}, d.get("inventory") or {}
                bc = f"{b['chg']*100:+.2f}pct" if b.get("chg") is not None else "N/A"
                pc = f"{p['chg']:+,.0f}" if p.get("chg") is not None else "N/A"
                ic = f"{iv['chg_ratio']*100:+.1f}%" if iv.get("chg_ratio") is not None else "N/A"
                gp = "；".join(d.get("gaps") or []) or "—"
                # M4：优先用基本面 Agent 综合评分（三分项基分 + 板块宏观传导）
                ag = agent_map.get(r) or {}
                sc = ag.get("score") if ag else None
                if sc is None:
                    sc = d.get("score")
                conf = ag.get("confidence") or "-"
                cov = ag.get("coverage")
                ccell = f"{conf}" + (f"（{cov}/3）" if cov is not None else "")
                L.append(f"| {d.get('name') or r} {r} | {_sym_by_root.get(r) or '-'} | "
                         f"{(sc or 0):+.2f} "
                         f"{'🔴偏多' if (sc or 0) > 0 else ('🟢偏空' if (sc or 0) < 0 else '⚪中性')} | "
                         f"{ccell} | {bc} | {pc} | {ic} | {gp} |")
            L.append("")
            if agent_map:
                L.append("> 评分优先取 **基本面/宏观/情绪 Agent（M4）** 综合分（三分项基分 + 板块宏观传导调节）；"
                         "「置信」= 三分项数据完备度（3/3 高、2/3 中、≤1 低）。"
                         "完整报告见 `E:\\QH\\期货简报\\简报内容\\基本面Agent报告_*.md`。\n")
            # 关键品种要点（前 5）
            _top = [(r, fund_map[r]) for r in _froots[:5] if (fund_map.get(r) or {}).get("parts")]
            if _top:
                L.append("**本期基本面要点（按影响排序）：**\n")
                for r, d in _top:
                    _ag = agent_map.get(r) or {}
                    _sc = _ag.get("score") if _ag else None
                    if _sc is None:
                        _sc = d.get("score")
                    _cf = _ag.get("confidence")
                    _cx = f"，置信 {_cf}" if _cf else ""
                    L.append(f"- **{d.get('name') or r} {r}**（评分 {_sc:+.2f}{_cx}）："
                             + "；".join(d.get("parts") or []))
                L.append("")
        else:
            L.append("- 本期无可操作信号/持仓对应的基本面数据。\n")

    # ================= 六、研究层：多空辩论 × 量化验证（M3） =================
    if res_map:
        L.append(f"## {CN[sec]}、研究层：多空辩论 × 量化验证\n")
        sec += 1
        L.append("> 口径：① **多空论据**由技术面（方向层/ADX/阶段/护栏）+ 基本面分项 + 宏观状态合成，"
                 "定性综述由 LLM 基于这些客观证据完成；② **量化验证**用 `fut_kline` 日线主连回放融合规则，"
                 "给出胜率/期望R/回撤(R)/近1年稳定性，用于**有效性背书**（不用于给点位）；"
                 "③ 三方**冲突即标红**，按框架要求不自动采信。\n")
        _rroots = []
        for x in display_results:
            if x["root"] in res_map and x["root"] not in _rroots:
                _rroots.append(x["root"])
        if len(active_df):
            for _, r in active_df.iterrows():
                rr0 = POS.norm_root(str(r["代码"]).strip())
                if rr0 in res_map and rr0 not in _rroots:
                    _rroots.append(rr0)
        _rroots.sort(key=lambda r: -abs(((res_map.get(r) or {}).get("bull_score") or 0)
                                        - ((res_map.get(r) or {}).get("bear_score") or 0)))
        # 显示名/活跃合约优先取当日合约级结果（研究层内部 name 是主连名，如「棕榈油连续」）
        _rname, _rsym = {}, {}
        for x in display_results:
            _rname.setdefault(x["root"], (x.get("name") or "").replace("连续", ""))
            _rsym.setdefault(x["root"], x.get("symbol"))
        if len(active_df):
            for _, _rr in active_df.iterrows():
                _rname.setdefault(POS.norm_root(str(_rr["代码"]).strip()), _rr.get("品种"))
                _rsym.setdefault(POS.norm_root(str(_rr["代码"]).strip()), str(_rr["代码"]).strip())
        if _rroots:
            L.append("| 品种 | 动作 | 研究结论 | 多头强度 | 空头强度 | 量化(n/胜率/期望R) | 近1年 | 冲突标记 |")
            L.append("|------|------|----------|----------|----------|---------------------|-------|----------|")
            for r in _rroots[:15]:
                v = res_map.get(r) or {}
                q = v.get("quant") or {}
                if q.get("n"):
                    qtxt = (f"{q['n']}笔 / {q.get('win',0)*100:.0f}% / {q.get('expR',0):+.2f}R"
                            f" {'✔' if q.get('valid') else '✘'}")
                    rtxt = f"n={q.get('recent_n',0)} {q.get('recent_expR',0):+.2f}R"
                else:
                    qtxt, rtxt = "N/A", "N/A"
                fl = "；".join((v.get("flags") or [])[:1]) or "—"
                _nm = _rname.get(r) or (v.get("name") or "").replace("连续", "") or r
                _sy = _rsym.get(r) or ""
                _cell = f"{_nm} {r}" + (f"·{_sy}" if _sy and _sy != r else "")
                L.append(f"| {_cell} | {v.get('action') or '-'} | **{v.get('verdict')}** | "
                         f"{v.get('bull_score')} | {v.get('bear_score')} | {qtxt} | {rtxt} | {fl} |")
            L.append("")
            # 关键品种的多空要点
            for r in _rroots[:3]:
                v = res_map.get(r) or {}
                if not (v.get("bull") or v.get("bear")):
                    continue
                _nm2 = _rname.get(r) or (v.get("name") or "").replace("连续", "") or r
                L.append(f"**{_nm2} {r}·多空辩论要点：**")
                for b in (v.get("bull") or [])[:3]:
                    L.append(f"- 🟩 多：{b}")
                for b in (v.get("bear") or [])[:3]:
                    L.append(f"- 🟥 空：{b}")
                L.append("")
        else:
            L.append("- 本期无可操作信号/持仓对应的研究层结论。\n")

    # ================= 七、纪律提醒 =================
    L.append(f"## {CN[sec]}、纪律提醒（复盘时逐条核对）\n")
    for s in [
        f"1. 单笔风险 ≤ 账户 {cfg['account']['单笔风险']*100:.0f}%、月亏 ≤{cfg['account']['月亏上限']*100:.0f}%（触及即本月停止交易）触发即停；",
        "2. 亏损仓不加仓不摊平；盈利仓金字塔加码最多一次；",
        "3. 震荡期空仓不是错过——引擎未触发的品种，观望是规则不是懒惰；",
        "4. 所有点位**以收盘价界定、次日开盘执行**；止损止盈只在收盘确认后动作，不盘中抢跑；",
        "5. 连亏 5–8 笔是趋势系统的正常成本；判断系统是否失效只看信号是否按规则触发，不看盈亏；",
        "6. 持仓品种触及止盈/止损、或引擎方向反转，必须**次日开盘无条件执行**，先平后反手；",
        "7. 手续费计入净浮盈后再判断盈亏，避免「看起来赚、扣费后亏」；",
        f"8. 仓位纪律：同时持仓不超过 **{MAX_POS}** 个；若当日可操作信号多于剩余额度，按反追高优先级"
        f"取前几个执行、其余标「观察」不新开仓，避免 49 品种同持的噪声与相关性拖垮边缘"
        f"（回测验证：按置信度集中选最热趋势反而亏损，故用分散子集）；"
        f"限仓是**风险控制**手段，非盈利保证——融合引擎为趋势依赖，仅趋势年盈利，震荡年即便限仓仍亏。",
        f"9. 锁仓纪律（v2.1）：浮亏 ≥ **{LOCK_TRIG}×ATR** 且信号未反转才锁仓（锁早=把本会自愈的浮亏冻结成实现亏损）；"
        f"并发 ≤**{LOCK_MAX}** 个、同一持仓最多锁 **{LOCK_MAX_TRIES}** 次；"
        "锁仓后**不手动平单边**——组合回补保本平锁仓腿解锁，信号反转/转弱才双平；"
        "锁仓不是止损替代，崩溃跳空日的尾部保护才是它的价值。",
    ]:
        L.append(s)
    L.append("")

    # ================= 末尾：本期主线 + 护栏(iii-A) 警示清单（任务要求移至最后） =================
    if longs or shorts:
        L.append("## 附、本期主线\n")
        L.append(f"1. 本期**可操作信号**：做多 {len(longs)} 个、做空 {len(shorts)} 个（均为融合引擎）。"
                 f"最强多头：{longs[0]['name'].replace('连续','') if longs else '无'}"
                 f"{(' ' + longs[0].get('symbol','')) if longs else ''}；"
                 f"最强空头：{shorts[0]['name'].replace('连续','') if shorts else '无'}"
                 f"{(' ' + shorts[0].get('symbol','')) if shorts else ''}。")
        L.append(f"2. 引擎分布：融合 {len(longs)+len(shorts)} 个（方向层 EMA20 + A/B 回踩/突破入场 + 2×ATR 吊灯止损）。")
        L.append(f"3. 持仓台账 {len(active_df)} 条（未平仓，详见第四章）。" if len(active_df)
                 else "3. 当前无持仓记录。")
        L.append(f"4. **仓位纪律**：最多同时持仓 {MAX_POS} 个；今日按反追高优先级入选执行 **{len(selected_roots)}** 个"
                 f"（剩余额度 {slots}），其余 **{len(new_cands)-len(selected_roots)}** 个标「观察」不新开仓；"
                 f"已持仓 {held_cnt} 个计入额度。"
                 + (f"板块限额：同板块新开 ≤{SECTOR_CAP} 个（今日让位 {len(_sec_blocked)} 个）。" if SECTOR_CAP > 0 else "")
                 + "入选清单见总览表 ✅ 标记。")
        _chase_warn = [x for x in results
                       if x.get("action") in ("做多", "做空")
                       and x.get("reason") and ("追高警示" in x["reason"] or "杀低警示" in x["reason"])]
        if _chase_warn:
            L.append(f"5. **🛡 护栏(iii-A) 警示**：{len(_chase_warn)} 个品种信号仍在、但距 10 日突破参考点 > 1×ATR"
                     "（详见下方『🛡 护栏(iii-A) 今日警示清单』）。"
                     "实盘层面：--gap-chase=0.5% 已默认启用——明日开盘若同向跳空 >0.5% 自动放弃入场。")
        L.append("")

        _chase_warn_all = [x for x in results
                           if x.get("action") in ("做多", "做空")
                           and x.get("reason") and ("追高警示" in x["reason"] or "杀低警示" in x["reason"])]
        if _chase_warn_all:
            _chase_warn_all.sort(key=lambda r: r["root"])
            L.append("### 🛡 护栏(iii-A) 今日警示清单\n")
            L.append("机制：信号仍可操作，但 close 已远离 10 日突破参考点 > 1×ATR / 2%。"
                     "实盘层面 `--gap-chase 0.005` 默认启用——明日开盘若同向跳空 >0.5% 自动跳过本次入场（信号次日再评估）。"
                     f"本期共 **{len(_chase_warn_all)}** 个。\n")
            L.append("| 品种 | 引擎 | 原方向 | 现价 | 参考突破点 | 延伸(点) | 延伸×ATR | 延伸% | 警示等级 |")
            L.append("|------|------|--------|------|------------|----------|----------|--------|----------|")
            for x in _chase_warn_all:
                eng = x.get("engine") or FUSION_ENGINE
                rs = x.get("reason") or ""
                if "追高" in rs:
                    orig_dir = "做多"; anchor_label = "10 日低点 ll10"; anchor_v = x.get("ll10")
                    gap_pts = (x["close"] - anchor_v) if anchor_v else 0
                    level = "强警示" if (gap_pts/x["atr"] >= 2) else "中等"
                elif "杀低" in rs:
                    orig_dir = "做空"; anchor_label = "10 日高点 hh10"; anchor_v = x.get("hh10")
                    gap_pts = (anchor_v - x["close"]) if anchor_v else 0
                    level = "强警示" if (gap_pts/x["atr"] >= 2) else "中等"
                else:
                    orig_dir = "?"; anchor_label = "?"; anchor_v = None; gap_pts = 0; level = "?"
                gap_atr = gap_pts / x["atr"] if x.get("atr") else 0
                gap_pct = gap_pts / anchor_v if anchor_v else 0
                L.append(f"| {x['root']} | {eng} | {orig_dir} | {x['close']:,.0f} | "
                         f"{anchor_label} {anchor_v:,.0f} | {int(gap_pts)} | {gap_atr:.1f}× | "
                         f"{gap_pct*100:.1f}% | {level} |")
            L.append("")

    L.append("**免责声明：本报告仅供教学与决策参考，不构成投资建议；期货有杠杆，风险自负。历史回测不代表未来收益。**\n")
    L.append(f"*生成于 {today} · 分析框架：Qi Analisy 融合策略 V3.4（日线口径）· 数据截至当日收盘*")
    if pending:
        L.append(f"\n> 注：以下品种行情接口暂未取到，技术指标待恢复后补算：{', '.join(map(str, pending))}")
    return "\n".join(L)


if __name__ == "__main__":
    main()
