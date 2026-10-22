# -*- coding: utf-8 -*-
"""融合策略 V5X —— 生产参考引擎（可执行的程序化策略）

版本冻结：2026-10-18；持仓周期参数于 2026-10-22 重锁（无 bug 代码全量扫描最优：
SL 3->1.5 / 吊灯 2->3.0 / BE 1.0 保持 / COOLDOWN 1->7 / ADD_THR 1.5 保持），
与 step46_prod_bt.py V5X 重锁版逐位对齐
（flags = {A1S, B3, M1a_pb, M5}, scope = P4_SECTORS），文末 self_check 用真实
生产库数据回放，与 trades46_V5X.csv 逐笔对拍。

策略一句话：
  60m K线，EMA140 定方向；EMA20 回踩（斐波那契汇流确认）或 10 根突破入场；
  五道研究门控过滤（高波日禁开仓 / 化工只回踩 / 尾盘根禁开仓 / MACD柱同号 /
  会员净持仓 |z|>=1.0）；1.75ATR 初始止损 + 2.0ATR 吊灯 + 1.0ATR 保本；
  阶梯加码最多 2 手；平仓后冷却 7 根。

生产接线方式：
  1. 每个交易日盘前：构造 DailyCtx 并 set_daily_ctx（hvol / member_z /
     簇冲击封禁 / 会话末根小时），所有输入只用 T-1 及以前的数据。
  2. 每根 60m bar 收盘后：engine.on_bar(...) -> [Order, ...]，
     订单在下一根 bar 开盘执行（回测按信号根收盘价成交，成本模型已含双边
     滑点 slip_ticks，实盘成交偏差在该容差内）。
  3. 引擎无任何前视：所有指标递归增量计算，决策只用 <= 当根收盘的信息。
"""
from dataclasses import dataclass
import numpy as np

# ============================================================
# 一、参数配置（全部冻结，改动需重新回测）
# ============================================================

@dataclass
class V5XConfig:
    # --- 信号层 ---
    EMA_DIR: int = 140          # 方向过滤 EMA（取前一根的方向）
    MA_N: int = 20              # 回踩/突破基准 EMA20
    ATR_N: int = 14             # ATR（前 14 根 TR 均值播种，之后 1/n 递推）
    ADX_N: int = 14             # ADX（内部 EMA alpha=2/(n+1)，前 28 根输出 0）
    ADX_MIN: float = 15.0       # 趋势强度下限（取前一根 ADX）
    BK_N: int = 10              # 突破支路：前 10 根最高/最低（不含当根）
    W_PIVOT: int = 60           # 枢轴点回看窗口
    FIB_RATIOS: tuple = (0.382, 0.5, 0.618)   # 斐波那契回撤位
    FIB_TOL: float = 0.5        # 汇流容差 = 0.5 * ATR
    # --- 离场层 ---
    SL_ATR: float = 1.75         # 初始止损 = 均价 ∓ 1.75*ATR  (V5X.2 重扫最优：1.5->1.75)
    TRAIL_ATR: float = 2.0      # 吊灯移动止损 = 峰值/谷值收盘 ∓ 2.0*ATR (V5X.2：3.0->2.0)
    BE_R: float = 1.0           # 浮盈 >= 1.0*ATR 后止损抬到入场价（保本）(2026-10-22 锁定：0.5->1.0)
    TP_R: float = None          # 实验开关：固定 R 止盈（None=关闭=回测口径）。
    # --- 加码 ---
    ADD_MAX_LOTS: int = 2       # 最大手数（首仓 1 + 加码 1）
    ADD_THR: float = 1.5        # 加码：浮盈 >= 当前手数*1.5*ATR 且收盘创新高/低 (2026-10-22 锁定：1.0->1.5)
    # --- 节奏 ---
    COOLDOWN: int = 7           # 平仓后冷却（2026-10-22 重锁：1->7，离场后更充分冷却以抑制过拟合噪声）
    ENTRY_MIN_I: int = 30       # 前 30 根不开仓（指标预热）
    # --- 门控 ---
    M5_ON: bool = True          # 会员净持仓强度门（跨品种体制门）；单品种可关
    M1A_ON: bool = True         # 回踩支路 MACD 柱同号门；单品种可关
    DIAGNOSE: bool = False       # 漏斗诊断（默认关，不影响 self_check）
    M5_Z_THR: float = 1.0       # 会员净持仓 |z| 阈值（0.5/1.0/1.5 敏感性甜点）
    # --- 体制门替代（替代 M5 的跨品种会员数据，用单品种可计算量）---
    ALT_GATE: str = 'm5'        # 'm5'(默认,尊重M5_ON) | 'none'(去体制门) |
                              # 'emadist'(价-EMA140偏离z) | 'mom'(N根动量) |
                              # 'adx'(ADX升阈) | 'dirpersist'(方向持续性) | 'mom_or_emadist'
    ALT_THR: float = 1.0        # 通用阈值：emadist=ATR倍数 | mom=收益率 | adx=ADX值 | dirpersist=根数
    ALT_N: int = 20             # mom 回看根数（60m）
    HVOL_Q: float = 0.80        # 日线振幅 250 日 80 分位（min_periods=60）
    CLUSTER_SHOCK_K: float = 3.0  # 簇冲击：|簇等权日收益| > 3σ(250d) 当日全簇封禁
    # --- 自进化体制层开关（默认全关，确保与已验证基线逐笔对拍完全一致）---
    REGIME_P2_AUTO: bool = False   # True：regime==2(高波) 时自动禁开仓（替代/扩展静态 hvol 规则）
    REGIME_POS_SCALE: bool = False # True：用 DailyCtx.pos_scale 缩放初始手数（不碰方向 alpha）

# P4「化工只回踩」适用范围（第二章证据所在链条）
P4_SECTORS = {'聚酯', '聚烯烃', '两碱', '醇醚'}

CLUSTER = {
    'TA': '聚酯', 'PX': '聚酯', 'PF': '聚酯', 'PR': '聚酯',
    'PP': '聚烯烃', 'L': '聚烯烃', 'V': '聚烯烃', 'EB': '聚烯烃',
    'FG': '两碱', 'SA': '两碱', 'SH': '两碱',
    'MA': '醇醚', 'EG': '醇醚', 'UR': '醇醚',
    'RB': '黑色', 'HC': '黑色', 'I': '黑色', 'J': '黑色', 'JM': '黑色',
    'SF': '黑色', 'SM': '黑色', 'SS': '黑色',
    'P': '油脂', 'Y': '油脂', 'OI': '油脂',
    'M': '粕类', 'RM': '粕类',
    'CU': '有色', 'AL': '有色', 'ZN': '有色', 'PB': '有色', 'NI': '有色',
    'SN': '有色', 'AO': '有色',
    'AU': '贵金属', 'AG': '贵金属',
    'SC': '能源', 'FU': '能源', 'LU': '能源', 'BU': '能源', 'PG': '能源',
    'CF': '软商品', 'SR': '软商品', 'AP': '软商品', 'JD': '软商品',
    'LH': '软商品', 'PK': '软商品',
    'A': '谷物', 'B': '谷物', 'C': '谷物', 'CS': '谷物',
    'RU': '橡胶', 'BR': '橡胶',
    'LC': '新能源', 'SI': '新能源', 'PS': '新能源',
    'SP': '纸浆', 'EC': '航运',
}


@dataclass
class CostCfg:
    """单品种成本：fee_row[act]=(fixed元/手, pct比例)；slip_yuan=slip_ticks*tick"""
    fee_row: dict               # {'OPEN':(fx,pc),'CLOSE_TODAY':(fx,pc),'CLOSE_YEST':(fx,pc)}
    slip_yuan: float            # 单边滑点（元/手）
    mult: float                 # 合约乘数
    tick: float                 # 最小变动价位

    def fee(self, act, px_raw):
        fx, pc = self.fee_row[act]
        return fx + pc * px_raw * self.mult

    def cost_round(self, entry_raw, exit_raw, same_day, lots):
        cx = self.fee('CLOSE_TODAY' if same_day else 'CLOSE_YEST', exit_raw)
        return (self.fee('OPEN', entry_raw) + cx + 2 * self.slip_yuan * self.mult) * lots


@dataclass
class DailyCtx:
    """每日盘前更新的上下文（全部只用 T-1 及以前信息）"""
    td: object                  # 当前交易日（夜盘已归次日）
    hvol: bool = False          # P2：昨日振幅 > 250日80分位 → 当日禁开仓
    member_z: float = np.nan    # M5：最新 available_at <= td 的会员净持仓 z（NaN=无数据，放行）
    blocked: bool = False       # 簇冲击日：当日全簇禁开仓
    sess_last_hours: frozenset = frozenset()  # P6：会话末根小时集合（如 {14} / {14,22}）
    # --- 自进化体制层（默认 None / 1.0，关闭时行为与原版完全一致）---
    regime: int = None         # 在线波动体制：0=低波 1=中波 2=高波（高波→可触发自动 P2 禁开仓）
    pos_scale: float = 1.0      # 体制驱动仓位缩放（初始手数乘数；默认 1.0 不改基线）


# ============================================================
# 二、增量指标（递推口径与回测/pandas 严格一致）
# ============================================================

class EMA:
    """alpha=2/(n+1)，首值播种 —— 等价 pd.Series.ewm(span=n, adjust=False)。
    NaN 输入：跳过不更新状态，输出前值（与 pandas 一致；未播种时输出 NaN）。"""
    def __init__(self, n):
        self.a = 2.0 / (n + 1.0); self.v = None
    def push(self, x):
        if x != x:                       # NaN
            return self.v if self.v is not None else np.nan
        self.v = x if self.v is None else self.a * x + (1 - self.a) * self.v
        return self.v


class ATR:
    """前 n 根 TR 均值播种，之后 alpha=1/n 递推；未播种返回 None。
    NaN bar：跳过不更新（刻意偏离 step46 的 poison-forever —— 那是僵尸仓 bug 根源）。"""
    def __init__(self, n):
        self.n = n; self.buf = []; self.v = None; self.pc = None
    def push(self, h, l, c):
        if h != h or l != l or c != c:
            return self.v                # 跳过 NaN bar，状态冻结
        pc = c if self.pc is None else self.pc
        tr = max(h - l, abs(h - pc), abs(l - pc))
        self.pc = c
        if self.v is None:
            self.buf.append(tr)
            if len(self.buf) == self.n:
                self.v = float(np.mean(self.buf)); self.buf = None
                return self.v
            return None
        self.v = (1.0 / self.n) * tr + (1 - 1.0 / self.n) * self.v
        return self.v


class ADX:
    """tr/+dm/-dm 各过 EMA(span=14)（EMA 递推非 Wilder），dx 再过 EMA(span=14)；
    前 2*n 根输出 0.0。NaN bar：状态冻结，输出上一次的值。"""
    def __init__(self, n):
        self.n = n; self.e_tr = EMA(n); self.e_p = EMA(n); self.e_m = EMA(n)
        self.e_dx = EMA(n); self.ph = None; self.pl = None; self.pc = None
        self.i = 0; self.last = 0.0
    def push(self, h, l, c):
        i = self.i; self.i += 1
        if h != h or l != l or c != c:
            return 0.0 if i < 2 * self.n else self.last
        if self.ph is None:
            pc = c; tr = max(h - l, abs(h - pc), abs(l - pc)); pdm = mdm = 0.0
        else:
            tr = max(h - l, abs(h - self.pc), abs(l - self.pc))
            up = h - self.ph; dn = self.pl - l
            pdm = up if (up > dn and up > 0) else 0.0
            mdm = dn if (dn > up and dn > 0) else 0.0
        self.ph, self.pl, self.pc = h, l, c
        atr_ = self.e_tr.push(tr); pp = self.e_p.push(pdm); mm = self.e_m.push(mdm)
        pdi = 100 * pp / (atr_ + 1e-9); mdi = 100 * mm / (atr_ + 1e-9)
        dx = 100 * abs(pdi - mdi) / (pdi + mdi + 1e-9)
        ad = self.e_dx.push(dx)
        out = 0.0 if i < 2 * self.n else ad
        self.last = out
        return out


class MACDHist:
    """hist = 2*(DIF-DEA)，DIF=EMA12-EMA26，DEA=EMA9(DIF)。NaN bar 输出前值。"""
    def __init__(self):
        self.e12 = EMA(12); self.e26 = EMA(26); self.e9 = EMA(9)
        self.last = np.nan
    def push(self, c):
        if c != c:
            return self.last
        dif = self.e12.push(c) - self.e26.push(c)
        self.last = 2.0 * (dif - self.e9.push(dif))
        return self.last


class VolRatio:
    """vol / median(前 20 根量, min_periods=5)，shift(1)：当根用历史窗中位数。NaN 量跳过。"""
    def __init__(self, n=20):
        self.n = n; self.buf = []
    def push(self, v):
        if v != v:
            return np.nan
        vr = np.nan
        if len(self.buf) >= 5:
            med = float(np.median(self.buf[-self.n:]))
            vr = v / max(med, 1e-9) if med > 0 else np.nan
        self.buf.append(v)
        return vr


# ============================================================
# 三、单品种引擎
# ============================================================

class SymbolEngine:
    """状态机：state 0 空仓 / 1 多 / 2 空。每根 bar 收盘调 on_bar 一次。"""

    def __init__(self, sym, cfg: V5XConfig, cost: CostCfg):
        self.sym = sym; self.cfg = cfg; self.cost = cost
        vc = sym[:-3] if sym.endswith('888') else sym
        self.cluster = CLUSTER.get(vc)
        # P4：化工链条只做回踩支路（突破支路在化工链为负期望）
        self.pullback_only = self.cluster in P4_SECTORS
        # 指标
        self.ema_dir = EMA(cfg.EMA_DIR); self.ema20 = EMA(cfg.MA_N)
        self.atr = ATR(cfg.ATR_N); self.adx = ADX(cfg.ADX_N)
        self.macd = MACDHist(); self.volr = VolRatio()
        # 序列缓存（10 年 60m 约 2 万根/品种，内存可忽略）
        self.o = []; self.h = []; self.l = []; self.c = []
        self.e_dir_hist = []; self.e20_hist = []
        self.ph = []; self.pl = []   # 窗内枢轴点索引
        self.i = -1
        self.prev_adx = 0.0; self.prev_hist = np.nan
        self.dir_run = 0; self.prev_dir_b = 0   # 方向持续性（DIR_PERSIST 体制门用）
        self.ctx = DailyCtx(td=None)
        # 持仓
        self.state = 0
        self.entry_px = self.avg_px = self.e_atr = 0.0
        self.lots = 1.0; self.entry_i = -1; self.cooldown = 0
        self.pos_unit = 1.0        # 体制仓位单位（默认 1.0；REGIME_POS_SCALE 开启时按 ctx.pos_scale 缩放）
        self.peak = self.trough = 0.0; self.be_done = False
        self.entry_td = None; self.entry_raw = 0.0
        self.trades = []

    def set_daily_ctx(self, ctx: DailyCtx):
        self.ctx = ctx

    def on_bar(self, ts, o, h, l, c, v, raw_c, allow=True):
        """bar 收盘后调用。allow=False 表示预热期/停牌期，只推指标不开仓。
        返回订单事件列表（OPEN/ADD/CLOSE）。"""
        cfg = self.cfg
        if cfg.DIAGNOSE and not hasattr(self, '_stat'):
            self._stat = dict(flip=0, raw=0, p2=0, blk=0, m1a=0, m5=0, bars=0)
            self._last_dir = None
            self._stat_y = {}
            self._stat_m = {}
            self.cur_mo = None
            self._raw_list = []
            self._pending_raw = None
        if cfg.DIAGNOSE:
            self._stat['bars'] += 1
            yr = getattr(ts, 'year', None)
            if yr is None and isinstance(ts, str):
                yr = int(ts[:4])
            self.cur_yr = yr
            mo = getattr(ts, 'month', None)
            if mo is None and isinstance(ts, str):
                mo = int(ts[5:7])
            self.cur_mo = mo
        self.i += 1; i = self.i
        self.o.append(o); self.h.append(h); self.l.append(l); self.c.append(c)
        orders = []

        # ---- 指标递推（当根值）----
        e_dir = self.ema_dir.push(c); ma20 = self.ema20.push(c)
        atr_v = self.atr.push(h, l, c); adx_v = self.adx.push(h, l, c)
        hist_v = self.macd.push(c); self.volr.push(v)

        # ---- 枢轴点维护（检测 i-1 根，与回测循环同序）----
        nan_bar = (c != c)               # 缺数 bar：指标冻结，不做任何决策
        if i >= 2 and not nan_bar:
            if self.h[i-1] > self.h[i-2] and self.h[i-1] > self.h[i]:
                self.ph.append(i-1)
            if self.l[i-1] < self.l[i-2] and self.l[i-1] < self.l[i]:
                self.pl.append(i-1)
        while self.ph and self.ph[0] < i - cfg.W_PIVOT: self.ph.pop(0)
        while self.pl and self.pl[0] < i - cfg.W_PIVOT: self.pl.pop(0)
        if self.cooldown > 0:
            self.cooldown -= 1

        # ---- 前一根派生值（无前视纪律）----
        prev_dir = (1 if self.c[i-1] > self.e_dir_hist[i-1] else -1) if i >= 1 else None
        prev_adx = self.prev_adx
        prev_hist = self.prev_hist
        if prev_dir is not None:          # 维护方向持续性计数（DIR_PERSIST 体制门）
            if self.prev_dir_b != 0 and prev_dir == self.prev_dir_b:
                self.dir_run += 1
            else:
                self.dir_run = 1
            self.prev_dir_b = prev_dir
        if cfg.DIAGNOSE and prev_dir is not None:
            if self._last_dir is not None and prev_dir != self._last_dir:
                self._inc('flip')
            self._last_dir = prev_dir

        # ============ 离场（先于进场；同根止损优先于止盈，保守口径）============
        if nan_bar:
            pass                         # 缺数 bar 不离场（持仓顺延）
        elif self.state == 1 and self.e_atr > 0:
            if c > self.peak: self.peak = c
            st = self.avg_px - cfg.SL_ATR * self.e_atr
            t = self.peak - cfg.TRAIL_ATR * self.e_atr
            if t > st: st = t
            if (c - self.entry_px) >= cfg.BE_R * self.e_atr: self.be_done = True
            if self.be_done and self.entry_px > st: st = self.entry_px
            if c <= st:
                orders.append(self._exit(ts, c, raw_c, 1, 'stop'))
            elif cfg.TP_R is not None and c >= self.entry_px + cfg.TP_R * cfg.SL_ATR * self.e_atr:
                orders.append(self._exit(ts, c, raw_c, 1, 'tp'))
        elif self.state == 2 and self.e_atr > 0:
            if c < self.trough: self.trough = c
            st = self.avg_px + cfg.SL_ATR * self.e_atr
            t = self.trough + cfg.TRAIL_ATR * self.e_atr
            if t < st: st = t
            if (self.entry_px - c) >= cfg.BE_R * self.e_atr: self.be_done = True
            if self.be_done and self.entry_px < st: st = self.entry_px
            if c >= st:
                orders.append(self._exit(ts, c, raw_c, -1, 'stop'))
            elif cfg.TP_R is not None and c <= self.entry_px - cfg.TP_R * cfg.SL_ATR * self.e_atr:
                orders.append(self._exit(ts, c, raw_c, -1, 'tp'))

        # ============ 进场 ============
        entered = False
        if (not nan_bar and self.state == 0 and self.cooldown == 0 and i >= cfg.ENTRY_MIN_I
                and atr_v is not None and atr_v > 0 and prev_dir is not None and allow):
            hour = getattr(ts, 'hour', None)
            if hour is None:
                hour = int(str(ts)[11:13])
            if hour in self.ctx.sess_last_hours:
                sig = 0                      # P6：会话末根不开仓
            else:
                ok_l = prev_dir >= 0; ok_s = prev_dir <= 0
                up = c > ma20; dn = c < ma20
                cross_up = up and self.c[i-1] < self.e20_hist[i-1]
                cross_dn = dn and self.c[i-1] > self.e20_hist[i-1]
                s_tl = ok_l and up and (l <= ma20) and cross_up
                s_ts = ok_s and dn and (h >= ma20) and cross_dn
                hh10 = max(self.h[max(0, i-10):i]) if i >= 1 else -np.inf
                ll10 = min(self.l[max(0, i-10):i]) if i >= 1 else np.inf
                s_bkl = ok_l and c > hh10 and c > o
                s_bks = ok_s and c < ll10 and c < o
                if self.pullback_only:       # P4：化工链禁突破支路
                    s_bkl = s_bks = False
                if s_tl:                     # 斐波那契汇流确认（仅回踩支路）
                    s_tl = self._fib_ok_long(i, atr_v)
                if s_ts:
                    s_ts = self._fib_ok_short(i, atr_v)
                if prev_adx < cfg.ADX_MIN:   # ADX 门（前一根）
                    s_tl = s_ts = s_bkl = s_bks = False
                sig = 1 if (s_tl or s_bkl) else (-1 if (s_ts or s_bks) else 0)
                if cfg.DIAGNOSE and sig != 0: self._inc('raw')
                if cfg.DIAGNOSE and sig != 0: self._pending_raw = (ts, sig)
                # ---- 研究门控 ----
                if sig != 0 and self.ctx.hvol:      # P2 高波日禁开仓
                    sig = 0
                if cfg.DIAGNOSE and sig != 0: self._inc('p2')
                if sig != 0 and self.ctx.blocked:   # 簇冲击日封禁
                    sig = 0
                if cfg.DIAGNOSE and sig != 0: self._inc('blk')
                if sig != 0 and cfg.M1A_ON:        # M1a_pb：回踩支路 MACD 柱同号门
                    if s_tl and prev_hist < 0: sig = 0
                    if s_ts and prev_hist > 0: sig = 0
                if cfg.DIAGNOSE and sig != 0: self._inc('m1a')
                if sig != 0 and not self._regime_pass(sig, i, c, e_dir, atr_v, adx_v, prev_adx):
                    sig = 0
                if sig != 0 and cfg.REGIME_P2_AUTO and self.ctx.regime == 2:
                    sig = 0                      # 自进化体制层：高波态自动禁开仓（扩展 P2）
                if cfg.DIAGNOSE and sig != 0: self._inc('m5')
                if cfg.DIAGNOSE and self._pending_raw is not None:
                    self._raw_list.append((self._pending_raw[0], self._pending_raw[1], sig != 0))
                    self._pending_raw = None
                if sig != 0:
                    self.state = 1 if sig > 0 else 2
                    self.entry_px = self.avg_px = c
                    self.entry_raw = raw_c
                    self.pos_unit = self.ctx.pos_scale if (cfg.REGIME_POS_SCALE and self.ctx.pos_scale) else 1.0
                    self.lots = self.pos_unit; self.entry_i = i
                    self.e_atr = atr_v
                    self.peak = self.trough = c
                    self.be_done = False
                    self.entry_td = self.ctx.td
                    orders.append(dict(type='OPEN', dir=sig, price=c, lots=1.0,
                                       ts=ts, etype='pb' if (s_tl or s_ts) else 'bk',
                                       stop=self.avg_px - sig * cfg.SL_ATR * atr_v))
                    entered = True

        # ============ 加码（进场当根不加）============
        if not entered and self.state != 0 and self.lots < cfg.ADD_MAX_LOTS and self.e_atr > 0:
            if self.state == 1:
                hh = max(self.h[self.entry_i:i]) if i > self.entry_i else self.h[self.entry_i]
                if c > hh and (c - self.entry_px) >= self.lots * cfg.ADD_THR * self.e_atr:
                    self.avg_px = (self.avg_px * self.lots + c) / (self.lots + self.pos_unit)
                    self.lots += self.pos_unit
                    orders.append(dict(type='ADD', dir=1, price=c, lots=self.pos_unit, ts=ts))
            elif self.state == 2:
                ll = min(self.l[self.entry_i:i]) if i > self.entry_i else self.l[self.entry_i]
                if c < ll and (self.entry_px - c) >= self.lots * cfg.ADD_THR * self.e_atr:
                    self.avg_px = (self.avg_px * self.lots + c) / (self.lots + self.pos_unit)
                    self.lots += self.pos_unit
                    orders.append(dict(type='ADD', dir=-1, price=c, lots=self.pos_unit, ts=ts))

        # 缓存供下一根使用
        self.prev_adx = adx_v; self.prev_hist = hist_v
        self.e_dir_hist.append(e_dir); self.e20_hist.append(ma20)
        return orders

    def _inc(self, k):
        """漏斗计数：累加总计、当前年份、当前年月分桶（仅 DIAGNOSE 调用）。"""
        self._stat[k] += 1
        yd = self._stat_y.get(self.cur_yr)
        if yd is None:
            yd = dict(flip=0, raw=0, p2=0, blk=0, m1a=0, m5=0)
            self._stat_y[self.cur_yr] = yd
        yd[k] += 1
        md = self._stat_m.get((self.cur_yr, self.cur_mo))
        if md is None:
            md = dict(flip=0, raw=0, p2=0, blk=0, m1a=0, m5=0)
            self._stat_m[(self.cur_yr, self.cur_mo)] = md
        md[k] += 1

    def _regime_pass(self, sig, i, c, e_dir, atr_v, adx_v, prev_adx):
        """体制门：替代 M5（跨品种会员 |z|）。返回 True=通过（保留 sig）。
        默认 'm5' 走原逻辑且尊重 M5_ON；'none' 去掉体制门；其余为单品种可计算代理。"""
        cfg = self.cfg
        g = cfg.ALT_GATE
        if g == 'none':
            return True
        if g == 'm5':
            if not cfg.M5_ON:
                return True
            z = self.ctx.member_z
            return np.isnan(z) or abs(z) >= cfg.M5_Z_THR
        if g == 'emadist':                      # 价-EMA140 偏离 z（趋势运行中）
            if atr_v <= 0:
                return True
            return abs((c - e_dir) / atr_v) >= cfg.ALT_THR
        if g == 'mom':                          # N 根动量，方向与信号一致
            if i < cfg.ALT_N:
                return True
            base = self.c[i - cfg.ALT_N]
            if base <= 0:
                return True
            mom = c / base - 1.0
            return abs(mom) >= cfg.ALT_THR and np.sign(mom) == np.sign(sig)
        if g == 'adx':                          # ADX 升阈（趋势体制强度）
            return prev_adx >= cfg.ALT_THR
        if g == 'dirpersist':                   # 方向持续性（连续同向前视）
            return self.dir_run >= cfg.ALT_THR
        if g == 'mom_or_emadist':               # 组合：mom(ALT_THR) 或 emadist(固定2ATR)
            mom_ok = False
            if i >= cfg.ALT_N:
                base = self.c[i - cfg.ALT_N]
                if base > 0:
                    mom = c / base - 1.0
                    mom_ok = abs(mom) >= cfg.ALT_THR and np.sign(mom) == np.sign(sig)
            ema_ok = atr_v > 0 and abs((c - e_dir) / atr_v) >= 2.0
            return mom_ok or ema_ok
        return True

    # ---------- 内部 ----------
    def _fib_ok_long(self, i, atr_v):
        if not self.pl: return False
        sli = self.pl[-1]
        if i - sli < 2: return False
        seg = self.h[sli:i+1]
        shi = sli + int(np.argmax(seg))
        lg = float(self.h[shi]) - float(self.l[sli])
        if lg <= 0: return False
        shp = float(self.h[shi]); tol = self.cfg.FIB_TOL * atr_v
        return any(abs(float(self.l[i]) - (shp - r * lg)) <= tol for r in self.cfg.FIB_RATIOS)

    def _fib_ok_short(self, i, atr_v):
        if not self.ph: return False
        shj = self.ph[-1]
        if i - shj < 2: return False
        seg = self.l[shj:i+1]
        slj = shj + int(np.argmin(seg))
        lg = float(self.h[shj]) - float(self.l[slj])
        if lg <= 0: return False
        slp = float(self.l[slj]); tol = self.cfg.FIB_TOL * atr_v
        return any(abs(float(self.h[i]) - (slp + r * lg)) <= tol for r in self.cfg.FIB_RATIOS)

    def _exit(self, ts, fill, raw_x, dir_, reason='stop'):
        cfg = self.cfg
        raw = ((fill - self.avg_px) if dir_ == 1 else (self.avg_px - fill)) * self.lots * self.cost.mult
        same_day = (self.ctx.td == self.entry_td)
        cost = self.cost.cost_round(self.entry_raw, raw_x, same_day, self.lots)
        net = raw - cost
        lots = self.lots
        self.trades.append(dict(sym=self.sym, dir=dir_, edt=None, xdt=ts, P=self.avg_px, X=fill,
                                lots=lots, e_atr=self.e_atr, net=net, gross=raw,
                                r=net / (cfg.SL_ATR * self.e_atr * self.cost.mult),
                                reason=reason))
        self.state = 0; self.cooldown = cfg.COOLDOWN
        self.be_done = False; self.lots = 1.0
        return dict(type='CLOSE', dir=dir_, price=fill, lots=lots, ts=ts, net=net)


# ============================================================
# 四、自检：与 step46 V5X 回测逐笔对拍
# ============================================================

def self_check_detailed(symbols=('MA888', 'RB888')):
    """用 prod50 真实数据回放，核对引擎输出与 trades46_V5X.csv 逐位一致。
    返回结构化结果（不改 self_check 的打印行为），供 v5x_relock_guard 调用。"""
    import pandas as pd
    import step46_prod_bt as s46

    ba, raw, dd, var, cost, fac = s46.load_all()
    costmap = s46.build_cost_fn(cost, var)
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    cfg = V5XConfig()

    data = {}
    for s in symbols:
        b = s46.build_sym(s, ba, raw, dd, var)
        if b is not None:
            data[s] = b
    data = s46.attach_member_z(data, fac)
    shock = s46.cluster_shock_days(data)

    ref = pd.read_csv('trades46_V5X.csv')
    rows = []
    ok_all = True
    for sym, b in data.items():
        vc = sym[:-3]
        fee_row, slip, mult = costmap[vc]
        eng = SymbolEngine(sym, cfg, CostCfg(fee_row, slip, mult, tick.get(vc, 1.0)))
        cl = CLUSTER.get(vc)
        blocked = shock.get(cl, set()) if cl else set()
        sl_hours = frozenset(int(x) for x in b.loc[b.sess_last, 'hour'].unique())
        prev_td = [None]
        for row in b.itertuples():
            td = row.td
            if td != prev_td[0]:
                eng.set_daily_ctx(DailyCtx(
                    td=td, hvol=bool(row.hvol),
                    member_z=float(row.member_z) if not pd.isna(row.member_z) else np.nan,
                    blocked=td in blocked, sess_last_hours=sl_hours))
                prev_td[0] = td
            eng.on_bar(row.ts, float(row.open), float(row.high), float(row.low),
                       float(row.close), float(row.volume), float(row.raw_c),
                       allow=bool(row.allow))
        mine = pd.DataFrame(eng.trades)
        ref_s = ref[ref.sym == sym].reset_index(drop=True)
        n_mine, n_ref = len(mine), len(ref_s)
        net_mine = float(mine.net.sum()) if n_mine else 0.0
        net_ref = float(ref_s.net.sum()) if n_ref else 0.0
        max_dev = (float(np.abs(mine.X.values - ref_s.X.values).max())
                   if (n_mine and n_ref) else (0.0 if n_mine == n_ref else 1e9))
        match = (n_mine == n_ref) and (abs(net_mine - net_ref) < 1e-4 * max(1.0, abs(net_ref)))
        ok_all &= match
        rows.append(dict(sym=sym, n_mine=n_mine, n_ref=n_ref, net_mine=net_mine,
                         net_ref=net_ref, max_exit_dev=max_dev, pass_=bool(match)))
    return dict(pass_=bool(ok_all), symbols=rows)


def self_check(symbols=('MA888', 'RB888')):
    """用 prod50 真实数据回放，核对引擎输出与 trades46_V5X.csv 逐位一致。"""
    res = self_check_detailed(symbols)
    for r in res['symbols']:
        print(f"{r['sym']}: mine {r['n_mine']} 笔 / net {r['net_mine']:,.2f}  vs  "
              f"ref {r['n_ref']} 笔 / net {r['net_ref']:,.2f}  -> {'一致' if r['pass_'] else '不一致!'}")
        if r['n_mine'] and r['n_ref'] and r['pass_']:
            print(f"   逐笔离场价最大偏差: {r['max_exit_dev']:.6g}")
    print('SELF-CHECK:', 'PASS' if res['pass_'] else 'FAIL')
    return res['pass_']


if __name__ == '__main__':
    self_check()
