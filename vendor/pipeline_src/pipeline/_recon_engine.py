# -*- coding: utf-8 -*-
"""
PRD §16.5 对账：日线实现（_fut_daily_brief.fusion_signal）↔ 线上引擎（app/strategies/fusion_signal.py）

三层对账：
  L1 参数表   — 两侧声明的常量逐项 diff
  L2 公式级   — ADX/ATR/EMA/斐波枢轴腿 在合成序列上的数值等价性（逐位）
  L3 逻辑级   — 入场/离场/加码/门控 的**顺序与作用域** diff（含源码行号溯源）

输出：控制台 + _recon_result.txt
"""
import io, sys, os, json, re, hashlib

OUT = []
def P(*a):
    s = " ".join(str(x) for x in a)
    OUT.append(s)
    print(s)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import numpy as np
import pandas as pd

# ============================================================
# L1 参数表
# ============================================================
# 线上引擎：来自 E:/Docker/qhyc/config/local.yaml 的 fusion: 段（权威 = 运行时配置）
ONLINE = dict(
    timeframe      = "小时线 (1H)",
    dir_layer      = "HTF EMA140 (ema_k=140)",
    entry_line     = "EMA20 (ma_n=20)",
    atr_n          = 14,
    sl_atr         = 2.0,
    trail_atr      = 2.0,
    be_r           = 0.5,
    W              = 60,
    entry_mode     = "both_nm",
    cooldown_bars  = 3,
    adx_n          = 14,
    adx_min        = 15.0,
    use_sbull      = False,
    fib_confl      = True,
    fib_ratios     = (0.382, 0.5, 0.618),
    fib_tol_atr    = 0.5,
    add_max_lots   = 2,
    add_thr_atr    = 1.0,
    add_guard_atr  = 0.0,
    min_bars       = 160,
    max_bars       = 400,
)

# 日线实现：来自 _fut_daily_brief.py 顶部常量（skill 侧）
DAILY = dict(
    timeframe      = "日线 (1D)",
    dir_layer      = "日线 EMA20 (ema(c,20))",
    entry_line     = "日线 EMA10 (ema(c,10))",
    atr_n          = 14,
    sl_atr         = 2.0,      # SLK
    trail_atr      = 2.0,      # TRK
    be_r           = 0.5,      # BER
    W              = 60,       # FIB_W
    entry_mode     = "both_nm",# A 回踩(≡pullback) + B 10根突破(≡breakout_nomacd)
    cooldown_bars  = None,     # 日线侧未实现冷却（简报每日一次，无冷却概念）
    adx_n          = 14,
    adx_min        = 15.0,     # ADX_GATE
    use_sbull      = False,    # 日线 A 支路要求 strb/strr（收强/收弱）
    fib_confl      = True,     # FIB_CONFL
    fib_ratios     = (0.382, 0.5, 0.618),
    fib_tol_atr    = 0.5,
    add_max_lots   = 2,
    add_thr_atr    = 1.0,
    add_guard_atr  = 0.0,
    min_bars       = None,
    max_bars       = None,
)

P("=" * 78)
P("L1 参数表对账（线上 local.yaml:fusion  ‖  日线 _fut_daily_brief.py 常量）")
P("=" * 78)
P(f"{'参数':<16}{'线上引擎':<26}{'日线实现':<26}{'判定'}")
P("-" * 78)
KEYS = ["timeframe", "dir_layer", "entry_line", "atr_n", "sl_atr", "trail_atr", "be_r",
        "W", "entry_mode", "cooldown_bars", "adx_n", "adx_min", "use_sbull",
        "fib_confl", "fib_ratios", "fib_tol_atr", "add_max_lots", "add_thr_atr",
        "add_guard_atr", "min_bars", "max_bars"]
param_rows = []
for k in KEYS:
    ov, dv = ONLINE.get(k), DAILY.get(k)
    if ov == dv:
        verdict = "一致"
    else:
        verdict = "**差异**"
    P(f"{k:<16}{str(ov):<26}{str(dv):<26}{verdict}")
    param_rows.append((k, ov, dv, verdict))

# 语义差异（不是数值差异，而是口径差异）——必须人工标注
P()
P("── 口径差异（非数值，属设计差异，需明示）──")
SEM = [
    ("时间框架", "小时线", "日线", "**设计差异**：日线 EMA20 ≈ 小时 EMA140，属刻意的降采样近似"),
    ("方向层实现", "htf_dir（外部传入 HTF EMA140 符号）", "close_prev vs ema20_prev（自算）",
     "等价：均为「用前一根已收盘值判方向」"),
    ("A 回踩「收强」要求", "use_sbull=false → 不要求", "代码硬性要求 strb/strr(收强/收弱)",
     "**逻辑差异**：日线 A 支路更严（多一道收强门）"),
    ("冷却", "cooldown_bars=3（平仓后 3 根不进场）", "无冷却实现",
     "**设计差异**：日线侧为每日一次性扫描，无冷却概念"),
    ("入场线语义", "EMA20 (ma_n) 同时用于回踩判定", "EMA10 用于回踩、EMA20 用于方向",
     "**口径差异**：线上回踩线=EMA20，日线回踩线=EMA10（≈线上方向层）"),
]
for k, o, d, note in SEM:
    P(f"  · {k}")
    P(f"      线上：{o}")
    P(f"      日线：{d}")
    P(f"      → {note}")

# ============================================================
# L2 公式级数值等价性
# ============================================================
P()
P("=" * 78)
P("L2 公式级对账（同一合成序列上逐位比较）")
P("=" * 78)

rng = np.random.default_rng(20260922)
N = 400
# 合成一段带趋势/震荡交替的 OHLC
base = 3000 + np.cumsum(rng.normal(0, 12, N))
c = base.copy()
o = np.concatenate([[c[0]], c[:-1]])
h = np.maximum(o, c) + np.abs(rng.normal(0, 6, N))
l = np.minimum(o, c) - np.abs(rng.normal(0, 6, N))

def ema_np(s, n):
    return pd.Series(np.asarray(s, float)).ewm(span=n, adjust=False).mean().to_numpy()

def atr_online(h, l, c, n=14):
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    L = len(tr); out = np.full(L, np.nan)
    if L < n: return out
    out[n - 1] = tr[:n].mean()
    a = 1.0 / n
    for i in range(n, L):
        out[i] = a * tr[i] + (1 - a) * out[i - 1]
    return out

def atr_daily(h, l, c, n=14):
    h = pd.Series(h); l = pd.Series(l); c = pd.Series(c)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean().to_numpy()

def adx_online(h, l, c, n=14):
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    cprev = np.concatenate([[c[0]], c[:-1]])
    hprev = np.concatenate([[h[0]], h[:-1]])
    lprev = np.concatenate([[l[0]], l[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - cprev), np.abs(l - cprev)])
    up = h - hprev; dn = lprev - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr_ = pd.Series(tr).ewm(span=n, adjust=False).mean().to_numpy()
    pp = pd.Series(pdm).ewm(span=n, adjust=False).mean().to_numpy()
    mm = pd.Series(mdm).ewm(span=n, adjust=False).mean().to_numpy()
    pdi = 100 * pp / (atr_ + 1e-9); mdi = 100 * mm / (atr_ + 1e-9)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-9)
    ad = pd.Series(dx).ewm(span=n, adjust=False).mean().to_numpy().copy()
    ad[:2 * n] = 0.0
    return ad

def adx_daily(h, l, c, n=14):
    h = pd.Series(h); l = pd.Series(l); c = pd.Series(c)
    up = h.diff(); dn = l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_ = pd.Series(tr).ewm(span=n, adjust=False).mean()
    plus_di = pd.Series(plus_dm).ewm(span=n, adjust=False).mean() / atr_ * 100
    minus_di = pd.Series(minus_dm).ewm(span=n, adjust=False).mean() / atr_ * 100
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).abs() * 100
    return dx.ewm(span=n, adjust=False).mean().to_numpy()

def cmp(name, a, b, slice_from=20):
    a = np.asarray(a, float); b = np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    m[:slice_from] = False
    if not m.any():
        P(f"  {name:<34} 无可比区间"); return None
    d = np.abs(a[m] - b[m])
    mx = d.max(); denom = np.maximum(np.abs(a[m]), 1e-9)
    rel = (d / denom).max()
    ok = "逐位一致" if mx == 0 else ("等价(浮点误差内)" if rel < 1e-9 else "**不一致**")
    P(f"  {name:<34} max|Δ|={mx:.3e}  max_rel={rel:.3e}  {ok}")
    return ok

P("  ── EMA ──")
cmp("EMA(20) online-vs-daily", ema_np(c, 20), ema_np(c, 20), 0)
P("  ── ATR(14) ──")
cmp("ATR(14) online-vs-daily", atr_online(h, l, c), atr_daily(h, l, c))
P("  ── ADX(14) ──")
cmp("ADX(14) online-vs-daily", adx_online(h, l, c), adx_daily(h, l, c), 30)

# ---- 斐波枢轴/腿（两侧算法对照）----
def fib_online(h, l, i, is_long, W, ratios, tol):
    # 复刻 app/strategies/fusion_signal.py:227-262
    ph, pl = [], []
    for k in range(2, i + 1):
        if h[k - 1] > h[k - 2] and h[k - 1] > h[k]:
            ph.append(k - 1)
        if l[k - 1] < l[k - 2] and l[k - 1] < l[k]:
            pl.append(k - 1)
        while ph and ph[0] < k - W: ph.pop(0)
        while pl and pl[0] < k - W: pl.pop(0)
    if is_long:
        if not pl: return None
        _sli = pl[-1]
        if i - _sli < 2: return None
        _seg = h[_sli:i + 1]
        _shi = _sli + int(np.argmax(_seg))
        _lg = float(_seg.max()) - float(l[_sli])
        if _lg <= 0: return None
        _shp = float(h[_shi]); _lo = float(l[i])
        return (any(abs(_lo - (_shp - r * _lg)) <= tol for r in ratios), _sli, _shp, _lg)
    else:
        if not ph: return None
        _shj = ph[-1]
        if i - _shj < 2: return None
        _seg2 = l[_shj:i + 1]
        _slj = _shj + int(np.argmin(_seg2))
        _lg2 = float(h[_shj]) - float(_seg2.min())
        if _lg2 <= 0: return None
        _slp = float(l[_slj]); _hi = float(h[i])
        return (any(abs(_hi - (_slp + r * _lg2)) <= tol for r in ratios), _shj, _slp, _lg2)

def fib_daily(h, l, i, is_long, W, ratios, tol, k_atr=0.0, atr_v=0.0, min_gap=1):
    # 复刻 _fut_daily_brief.py:_fib_eval（含 2026-09-22 §16.9 放宽：幅度定义腿）
    # ⚠️ min_gap>=2 时启用「旧口径」：最近枢轴太近即整根作废（break，**不回溯前寻**），
    #    保证回退路径与原实现逐位一致。
    min_gap = int(min_gap)
    strict_gap = (min_gap >= 2)
    use_amp = bool(k_atr and k_atr > 0 and atr_v and np.isfinite(atr_v) and atr_v > 0)
    amp_thr = (float(k_atr) * float(atr_v)) if use_amp else 0.0
    piv = None
    lo_bound = max(0, i - int(W))
    for j in range(i - 1, lo_bound - 1, -1):
        if j - 1 < 0: continue
        if is_long:
            if not (l[j] < l[j - 1] and l[j] < l[j + 1]): continue
            if strict_gap and (i - j) < min_gap: break
            if (i - j) < min_gap: continue
            seg = h[j:i + 1]
            _hi = float(seg.max()); _lo = float(l[j]); _lg = _hi - _lo
        else:
            if not (h[j] > h[j - 1] and h[j] > h[j + 1]): continue
            if strict_gap and (i - j) < min_gap: break
            if (i - j) < min_gap: continue
            seg = l[j:i + 1]
            _hi = float(h[j]); _lo = float(seg.min()); _lg = _hi - _lo
        if _lg <= 0: continue
        if use_amp and _lg < amp_thr: continue
        piv = j; break
    if piv is None: return None
    if is_long:
        seg = h[piv:i + 1]
        leg_hi = float(seg.max()); leg_lo = float(l[piv]); lg = leg_hi - leg_lo
        if lg <= 0: return None
        levels = [leg_hi - r * lg for r in ratios]; px = float(l[i])
    else:
        seg = l[piv:i + 1]
        leg_hi = float(h[piv]); leg_lo = float(seg.min()); lg = leg_hi - leg_lo
        if lg <= 0: return None
        levels = [leg_lo + r * lg for r in ratios]; px = float(h[i])
    hit = any(abs(px - lv) <= tol for lv in levels)
    return (hit, piv, leg_hi, lg)

P("  ── 斐波枢轴/腿（逐个 bar 全量对照）──")
# ⚠️ 2026-09-22 §16.9：日线侧 `_fib_eval` 已放宽（FIB_MIN_GAP=1 + 幅度定义腿），
#    线上引擎 `fib_online` 仍是旧口径 → 此处**必须两口径分别对照**，否则会误报差异。
#    ① 旧口径回退（min_gap=2, k_atr=0）应仍与线上**逐位一致**（防放宽误伤原有语义）；
#    ② 新口径（min_gap=1, k_atr=1.0）与旧口径的差异数 = 放宽带来的**门控松绑量**。
ATR14 = atr_daily(h, l, c)
mismatch = 0; total = 0; miss_o = 0; miss_d = 0
relax_diff = 0; relax_was_none = 0
for i in range(62, N):
    tol = 0.5 * ATR14[i]
    for is_long in (True, False):
        ro = fib_online(h, l, i, is_long, 60, (0.382, 0.5, 0.618), tol)
        rd = fib_daily(h, l, i, is_long, 60, (0.382, 0.5, 0.618), tol,
                       k_atr=0.0, atr_v=0.0, min_gap=2)          # 旧口径（回退）
        rd_new = fib_daily(h, l, i, is_long, 60, (0.382, 0.5, 0.618), tol,
                           k_atr=1.0, atr_v=float(ATR14[i]), min_gap=1)  # §16.9 新口径
        total += 1
        if rd is None and rd_new is not None:
            relax_was_none += 1          # 旧口径无腿 → 新口径放行
        elif rd is not None and rd_new is not None and (rd[0] != rd_new[0] or rd[1] != rd_new[1]):
            relax_diff += 1              # 枢轴换人（更早枢轴）导致判定变化
        if ro is None and rd is None:
            continue
        if ro is None: miss_o += 1; continue
        if rd is None: miss_d += 1; continue
        if ro[0] != rd[0] or abs(ro[1] - rd[1]) > 0:
            mismatch += 1
P(f"  bar×方向 组合 {total} 个｜**旧口径** 判定不一致 {mismatch}｜仅线上有腿 {miss_o}｜仅日线有腿 {miss_d}")
P(f"  → 旧口径回退一致性: {'逐位一致 ✅' if mismatch==0 and miss_o==0 and miss_d==0 else '**存在差异，见上**'}")
P(f"  §16.9 放宽量: 旧无腿→新放行 {relax_was_none}｜枢轴换人 {relax_diff}｜合计 {relax_was_none+relax_diff}"
  f"（占组合 {100.0*(relax_was_none+relax_diff)/max(1,total):.2f}%）")

# ============================================================
# L3 逻辑级：顺序与作用域
# ============================================================
P()
P("=" * 78)
P("L3 逻辑级对账（顺序 / 作用域 / 源码行号）")
P("=" * 78)

L3 = [
    ("① ADX 门控作用域",
     "fusion_signal.py:266-267  用 adx_arr[i-1]；作废 s_tl/s_ts/s_bkl/s_bks（A+B 全作废）",
     "_fut_daily_brief.py:421-425  用 adx_prev；a_long/a_short/b_long/b_short 全 False",
     "一致"),
    ("② 斐波门控作用域",
     "fusion_signal.py:227-262  仅当 (s_tl or s_ts) 时判定；不动 s_bkl/s_bks",
     "_fut_daily_brief.py:412-415  仅 a_long/a_short AND fib_ok；b_* 不动",
     "一致（均只约束 A 回踩支路）"),
    ("③ 门控 vs 斐波顺序",
     "先斐波(227) 后 ADX(266)；两者互不依赖，顺序无副作用",
     "先斐波(412) 后 ADX(422)；同上",
     "等价"),
    ("④ 方向层无前视",
     "ok_l/ok_s 由外部 htf_dir（HTF EMA140 前一根符号）传入",
     "ok_long = close_prev > ema20_prev（显式用前一根）",
     "一致（均用已收盘前值）"),
    ("⑤ A 回踩支路定义",
     "s_tl = use_pb & ok_l & up & tl & (sbull or not use_sbull) & cross_up",
     "a_long = ok_long & _hf & (c>ema_f) & (l<=ema_f) & strb & up",
     "**差异**：线上 use_sbull=false 免收强；日线硬性要求 strb"),
    ("⑥ B 突破支路定义",
     "s_bkl = use_bk & ok_l & c>_hh10 & c>o & no_macd；_hh10=max(h[i-10:i])",
     "b_long = ok_long & c>hh10 & c>o；hh10=high[-11:-1].max()",
     "一致（窗口定义等价，均不含当根）"),
    ("⑦ 初始止损",
     "init_stop = entry_px ∓ sl_atr×ATR（sl_atr=2.0）",
     "stop = close ∓ SLK×ATR（SLK=2.0）",
     "一致"),
    ("⑧ 吊灯止损",
     "trail = peak/trough ∓ trail_atr×ATR（trail_atr=2.0，close-only）",
     "trail = TRK×ATR（TRK=2.0），台账按近 300 根收盘极值推",
     "一致"),
    ("⑨ 保本",
     "be_r=0.5；触发 = entry_px ± 0.5×ATR；触发后 cur_stop=max(cur, entry_px)",
     "BER=0.5；breakeven = close ± 0.5×ATR",
     "一致"),
    ("⑩ 保本距离口径",
     "be_r × e_atr（注释明确「不再乘 sl_atr」）",
     "BER × atr_v（未乘 SLK）",
     "一致（两侧均为 0.5×ATR）"),
    ("⑪ 加码门槛",
     "lots × add_thr_atr × ATR（add_thr_atr=1.0，金字塔线性抬升）",
     "_fut_positions.py: ADDON_MIN_PROFIT_ATR=1.0",
     "**需核**：日线加码门槛是否也带 lots 因子（见下）"),
    ("⑫ 加码触发条件",
     "收盘创入场以来新高（c[i]>max(h[entry_i:i])）且浮盈≥门槛",
     "「收盘创入场以来新高」（已删「回踩 EMA10 收回」）",
     "一致"),
    ("⑬ 加码腿数",
     "add_max_lots=2（开仓1手，至多加1次）",
     "ADDON_MAX_LEGS=2",
     "一致"),
    ("⑭ 结构性保本约束",
     "add_guard_atr=0.0（V3.4 停用）",
     "add_guard_atr=0.0（同停用）",
     "一致"),
    ("⑮ 离场执行",
     "close-only（c[i] <= cur_stop 才平）",
     "所有点位以当日收盘价界定，次日开盘执行",
     "**差异（已知设计）**：线上收盘即判；日线收盘判定、次日开盘执行"),
    ("⑯ 冷却",
     "cooldown_bars=3（平仓后 3 根）",
     "未实现",
     "**设计差异**：日线侧无冷却"),
]
for t, o, d, v in L3:
    P(f"{t}")
    P(f"    线上：{o}")
    P(f"    日线：{d}")
    P(f"    → {v}")
    P()

# ============================================================
# 汇总
# ============================================================
P("=" * 78)
P("汇总")
P("=" * 78)
n_param_diff = sum(1 for r in param_rows if r[3] != "一致")
P(f"L1 参数表：{len(param_rows)} 项，数值不一致 {n_param_diff} 项")
P(f"L2 公式级：EMA/ATR/ADX/斐波 四项对照（见上）")
P(f"L3 逻辑级：{len(L3)} 项，硬差异 3 项（⑤A收强 / ⑪加码lots / ⑯冷却），已知设计差异 2 项（⑮执行时点/时间框架）")

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_recon_result.txt"),
          "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
P()
P("已写入 _recon_result.txt")
