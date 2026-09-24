"""
融合策略实时信号引擎（每15分钟扫描用）

与已验证回测口径逐位一致（额外执行口径，V3.4 定稿 2026-09-21）：
  - 方向层：小时线 EMA140，门控多/空（用前一根方向对齐，无前视）
  - 门控：ADX(14)≥15（用上一根判定）+ 斐波汇流（仅回踩支路，V3.2 起）
  - 入场：MA20 回踩重启（无收强要求）+ 10根突破（both_nm，去 MACD）
  - 离场：2×ATR 初始止损 + 2×ATR 吊灯移动止损 + 0.5×ATR 保本
  - 加码：P1 利弗莫尔阶梯加码至 add_max_lots 手（V3.4 由浮盈门槛 lots×add_thr_atr×ATR 门控）
  - 执行：收盘价判定（close-only），与回测"只看收盘价"完全一致

口径说明（2026-09-14 修正）：
  保本触发距离 = be_r × 入场ATR（= 0.5×ATR），**不再乘 sl_atr**。
  原因：旧式 be_r × sl_atr × ATR 在额外口径(sl_atr=2.0)下被动放大成 1.0×ATR，
  与文档 §5.1/§8、同花顺图 BEL(=BER×EAB) 不符。对应回测须传 be_dist='atr'。

本模块只关心"最后一根已收盘小时K 之后，策略应当处于的持仓状态"：
  state 0=空仓(FLAT) / 1=持多(LONG) / 2=持空(SHORT)。
状态机去重由 scheduler 结合持久化的 FusionPosition 完成（仅在状态变化时推送）。

EMA / ATR 实现与 mtf_engine 相同（ewm adjust=False / Wilder RMA）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, Boolean, DateTime, Float, Integer, JSON, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import get_engine
from app.core.logging import logger
from app.models.base import Base
from app.models import HourlyBar

# 全系统统一交易所时区（上海）；DB 会话已设为 Asia/Shanghai，
# 读出的 timestamptz 为上海感知，故收盘判定一律用上海 now。
_SH_TZ = ZoneInfo("Asia/Shanghai")


# -----------------------------------------------------
# 指标（与 mtf_engine 完全一致，避免跨模块依赖）
# -----------------------------------------------------
def ema(s, n: int):
    return pd.Series(np.asarray(s, float)).ewm(span=n, adjust=False).mean().to_numpy()


def atr14(h, l, c, n: int = 14):
    """Wilder RMA 的 ATR(n)。"""
    h = np.asarray(h, float)
    l = np.asarray(l, float)
    c = np.asarray(c, float)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    L = len(tr)
    out = np.full(L, np.nan)
    if L < n:
        return out
    out[n - 1] = tr[:n].mean()
    a = 1.0 / n
    for i in range(n, L):
        out[i] = a * tr[i] + (1 - a) * out[i - 1]
    return out


def adx14(h, l, c, n: int = 14):
    """Wilder ADX(n)：小时线趋势强度（只量强弱、不量方向）。

    与回测 `_fusion_adx_filter.adx14` 同式（ewm(adjust=False) 等价 Wilder RMA）。
    用途（V3.1）：入场门控——方向对但「根本没趋势」的震荡市假信号应当放弃。
    前 2n 根置 0，而入场要求 i>=30，故不会被误门控。
    """
    h = np.asarray(h, float)
    l = np.asarray(l, float)
    c = np.asarray(c, float)
    cprev = np.concatenate([[c[0]], c[:-1]])
    hprev = np.concatenate([[h[0]], h[:-1]])
    lprev = np.concatenate([[l[0]], l[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - cprev), np.abs(l - cprev)])
    up = h - hprev
    dn = lprev - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr_ = pd.Series(tr).ewm(span=n, adjust=False).mean().to_numpy()
    pp = pd.Series(pdm).ewm(span=n, adjust=False).mean().to_numpy()
    mm = pd.Series(mdm).ewm(span=n, adjust=False).mean().to_numpy()
    pdi = 100 * pp / (atr_ + 1e-9)
    mdi = 100 * mm / (atr_ + 1e-9)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-9)
    ad = pd.Series(dx).ewm(span=n, adjust=False).mean().to_numpy().copy()
    ad[:2 * n] = 0.0
    return ad


# -----------------------------------------------------
# 融合策略主循环：返回最后一根K收盘后的 state（0/1/2）
# 逻辑逐位复刻 mtf_engine_intrabar.run_symbol_intrabar（both_nm / close-only）
# -----------------------------------------------------
def fusion_state(o, h, l, c, htf_dir, p) -> int:
    """只取状态（0/1/2）。实际计算走 fusion_state_detail，保证单一真源、无口径分叉。"""
    return fusion_state_detail(o, h, l, c, htf_dir, p)["state"]


def walk_fusion_states(o, h, l, c, htf_dir, p):
    """逐根生成融合策略持仓明细（**单一真源**，与 `fusion_state_detail` 同一套循环）。

    在每根已收盘 bar `i` 上，只使用 `≤ i` 的数据推断该 bar 收盘后的持仓状态——
    天然 walk-forward、无前视（回测可直接逐帧消费，O(n) 一次成型）。

    Yields:
        dict（与 `fusion_state_detail` 返回结构完全一致）：
          state/entry_i/entry_px/entry_atr/peak/trough/be_done/be_trigger/init_stop/trail_stop/cur_stop
    """
    n = len(c)
    if n < 40:
        return
    ma20 = ema(c, p.ma_n)
    atr_arr = atr14(h, l, c, p.atr_n)
    ok_l = htf_dir >= 0
    ok_s = htf_dir <= 0

    # ---- V3.1 可选项（默认关闭 = 与旧行为逐位一致）----
    # adx_min > 0 时启用「趋势强度门控」：小时线 ADX(adx_n) 低于阈值则放弃本根入场。
    # use_sbull = False 时取消回踩支路的「收强/收弱」要求（V3.1 验证为有效改进）。
    adx_arr = adx14(h, l, c, int(getattr(p, "adx_n", 14)))
    adx_min = float(getattr(p, "adx_min", 0.0) or 0.0)
    use_sbull = bool(getattr(p, "use_sbull", True))

    # ---- V3.2 可选项（默认关闭 = 与 V3.1 逐位一致）----
    # P0 斐波那契·汇流：回踩极值须落在斐波位 ± tol×ATR 内（只作用于回踩支路，不动 EMA20 体系）。
    fib_confl = bool(getattr(p, "fib_confl", False))
    fib_ratios = tuple(getattr(p, "fib_ratios", (0.382, 0.5, 0.618)) or (0.382, 0.5, 0.618))
    fib_tol_atr = float(getattr(p, "fib_tol_atr", 0.5) or 0.5)
    # P1 利弗莫尔·阶梯加码：允许多手；V3.4 起由「浮盈门槛」门控——
    #   门槛 = lots × add_thr_atr × ATR（金字塔式随手数线性抬升）。
    #   V3.2 的「吊灯已推进到加码后均价之上」结构保本约束（add_guard_atr）自 V3.4 停用。
    add_max_lots = int(getattr(p, "add_max_lots", 1) or 1)
    add_guard_atr = float(getattr(p, "add_guard_atr", 0.0) or 0.0)  # V3.4 停用（保留字段兼容旧配置）
    add_thr_atr = float(getattr(p, "add_thr_atr", 1.0) or 1.0)

    ph: list[int] = []
    pl: list[int] = []
    state = 0
    entry_px = 0.0           # 首次买入价（= 加权均价在 lots==1 时相同）
    avg_px = 0.0             # 加权均价（加码后上移）
    lots = 1.0
    entry_i = -1
    cooldown = 0
    e_atr = 0.0
    peak = 0.0
    trough = 0.0
    be_done = False

    for i in range(2, n):
        if h[i - 1] > h[i - 2] and h[i - 1] > h[i]:
            ph.append(i - 1)
        if l[i - 1] < l[i - 2] and l[i - 1] < l[i]:
            pl.append(i - 1)
        while ph and ph[0] < i - p.W:
            ph.pop(0)
        while pl and pl[0] < i - p.W:
            pl.pop(0)
        if cooldown > 0:
            cooldown -= 1

        # ---------------- 离场（多空镜像，close-only） ----------------
        # 成本基准用加权均价 avg_px（未加码时 == entry_px，与 V3.1 逐位一致）。
        if state == 1:
            if c[i] > peak:
                peak = c[i]
            st = avg_px - p.sl_atr * e_atr
            if p.trail_atr > 0:
                t = peak - p.trail_atr * e_atr
                if t > st:
                    st = t
            if p.be_r > 0 and (c[i] - entry_px) >= p.be_r * e_atr:
                be_done = True
            if be_done and entry_px > st:
                st = entry_px
            if c[i] <= st:
                state = 0
                cooldown = p.cooldown_bars
                be_done = False
                lots = 1.0
                avg_px = entry_px
        elif state == 2:
            if c[i] < trough:
                trough = c[i]
            st = avg_px + p.sl_atr * e_atr
            if p.trail_atr > 0:
                t = trough + p.trail_atr * e_atr
                if t < st:
                    st = t
            if p.be_r > 0 and (entry_px - c[i]) >= p.be_r * e_atr:
                be_done = True
            if be_done and entry_px < st:
                st = entry_px
            if c[i] >= st:
                state = 0
                cooldown = p.cooldown_bars
                be_done = False
                lots = 1.0
                avg_px = entry_px

        # ---------------- 进场（state==0 且过冷却） ----------------
        if state == 0 and cooldown == 0 and i >= 30:
            up = c[i] > ma20[i]
            dn = c[i] < ma20[i]
            cross_up = c[i] > ma20[i] and c[i - 1] < ma20[i - 1]
            cross_down = c[i] < ma20[i] and c[i - 1] > ma20[i - 1]
            tl = l[i] <= ma20[i]
            sbull = c[i] > o[i] and c[i] >= 0.5 * (h[i] + l[i])
            ts = h[i] >= ma20[i]
            sbear = c[i] < o[i] and c[i] <= 0.5 * (h[i] + l[i])

            use_pb = p.entry_mode in ("pullback", "both", "both_nm")
            use_bk = p.entry_mode in ("breakout", "breakout_nomacd", "both", "both_nm")
            no_macd = p.entry_mode in ("breakout_nomacd", "both_nm")

            s_tl = use_pb and ok_l[i] and up and tl and (sbull or not use_sbull) and cross_up
            s_ts = use_pb and ok_s[i] and dn and ts and (sbear or not use_sbull) and cross_down
            _hh10 = max(h[max(0, i - 10):i])
            _ll10 = min(l[max(0, i - 10):i])
            s_bkl = use_bk and ok_l[i] and c[i] > _hh10 and c[i] > o[i] and no_macd
            s_bks = use_bk and ok_s[i] and c[i] < _ll10 and c[i] < o[i] and no_macd

            # ---- P0 斐波那契·汇流（只作用于回踩支路）----
            # 斐波"腿" = 最近一个**已确认枢轴** → 其后极值（回看窗口 W 内，天然无前视）。
            # 要求该根的逆势极值 l[i](多)/h[i](空) 落在 0.382/0.5/0.618 位 ± tol×ATR 内。
            if fib_confl and (s_tl or s_ts):
                _tol = fib_tol_atr * atr_arr[i]
                if s_tl:
                    _legl = None
                    if pl:
                        _sli = pl[-1]
                        if i - _sli >= 2:
                            _seg = h[_sli:i + 1]
                            _shi = _sli + int(np.argmax(_seg))
                            _lg = float(_seg.max()) - float(l[_sli])
                            if _lg > 0:
                                _legl = (_shi, _lg)
                    if _legl is None:
                        s_tl = False
                    else:
                        _shp = float(h[_legl[0]])
                        _lo = float(l[i])
                        if not any(abs(_lo - (_shp - r * _legl[1])) <= _tol for r in fib_ratios):
                            s_tl = False
                if s_ts:
                    _legs = None
                    if ph:
                        _shj = ph[-1]
                        if i - _shj >= 2:
                            _seg2 = l[_shj:i + 1]
                            _slj = _shj + int(np.argmin(_seg2))
                            _lg2 = float(h[_shj]) - float(_seg2.min())
                            if _lg2 > 0:
                                _legs = (_slj, _lg2)
                    if _legs is None:
                        s_ts = False
                    else:
                        _slp = float(l[_legs[0]])
                        _hi = float(h[i])
                        if not any(abs(_hi - (_slp + r * _legs[1])) <= _tol for r in fib_ratios):
                            s_ts = False

            # ADX 趋势强度门控（V3.1）：趋势不够强 → 本根全部入场信号作废。
            # 用「上一根」ADX 判定（i>=2 恒成立），与回测 bt_engine 完全一致、无前视。
            if adx_min > 0 and adx_arr[i - 1] < adx_min:
                s_tl = s_ts = s_bkl = s_bks = False

            if s_tl or s_bkl:
                state = 1
                entry_px = c[i]
                avg_px = c[i]
                lots = 1.0
                entry_i = i
                e_atr = atr_arr[i]
                peak = c[i]
                trough = c[i]
                be_done = False
            elif s_ts or s_bks:
                state = 2
                entry_px = c[i]
                avg_px = c[i]
                lots = 1.0
                entry_i = i
                e_atr = atr_arr[i]
                peak = c[i]
                trough = c[i]
                be_done = False

        # ---------------- P1 利弗莫尔·阶梯加码（V3.4：浮盈门槛门控） ----------------
        # 开仓恒为 1 手；仅当「收盘创入场以来新高/新低」且「浮盈 >= lots × add_thr_atr × ATR」
        # 才加 1 手（金字塔：门槛随手数线性抬升）。V3.2 的结构保本前提
        # （吊灯须已推进到加码后均价之上，add_guard_atr）自 V3.4 停用——
        # 实测该约束损失 19.6% 收益而回撤仅多 1.4 万（PRD §17.1 CR-5）。
        # 代价：加码后不再保证「最差=保本」，约 53% 加码单以亏损结束（中位仅 −0.16R）。
        elif add_max_lots >= 2 and state != 0 and lots < add_max_lots:
            if state == 1:
                _hh = max(h[entry_i:i]) if i > entry_i else h[entry_i]
                if c[i] > _hh and (c[i] - entry_px) >= lots * add_thr_atr * e_atr:
                    avg_px = (avg_px * lots + float(c[i])) / (lots + 1.0)
                    lots += 1.0
            elif state == 2:
                _ll = min(l[entry_i:i]) if i > entry_i else l[entry_i]
                if c[i] < _ll and (entry_px - c[i]) >= lots * add_thr_atr * e_atr:
                    avg_px = (avg_px * lots + float(c[i])) / (lots + 1.0)
                    lots += 1.0

        # ---------------- 汇总持仓价位（多空镜像） ----------------
        lv = _levels(state, avg_px, e_atr, peak, trough, be_done, p)
        yield {
            "state": state,
            "entry_i": entry_i if state != 0 else -1,
            "entry_px": avg_px if state != 0 else None,
            "first_px": entry_px if state != 0 else None,
            "lots": int(lots) if state != 0 else 0,
            "entry_atr": e_atr if state != 0 else None,
            "peak": peak if state == 1 else None,
            "trough": trough if state == 2 else None,
            "be_done": bool(be_done) if state != 0 else False,
            **lv,
        }


def fusion_state_detail(o, h, l, c, htf_dir, p) -> dict:
    """融合策略主循环，返回最后一根已收盘K之后的持仓**明细**（单一真源）。

    返回 dict：
      state      : 0=FLAT / 1=LONG / 2=SHORT
      entry_i    : 开仓那根K的索引（-1 表示空仓）
      entry_px   : **加权均价**（未加码时 == 首次买入价；加码后上移，P&L 的成本基准）
      first_px   : 首次买入价（保本触发与 0.5R 保本止损的基准，不受加码影响）
      lots       : 当前手数（1；启用 P1 加码后最多 add_max_lots）
      entry_atr  : 开仓当时的 ATR14（风险单位）
      peak/trough: 持仓期间收盘价的顺势极值（吊灯止损基准）
      be_done    : 是否已触发保本（止损已上移到成本价）
      be_trigger : 保本触发价 = first_px ± be_r×ATR（到达即挂保本）
      init_stop  : 初始止损 = entry_px ∓ sl_atr×ATR
      trail_stop : 吊灯止损 = peak/trough ∓ trail_atr×ATR
      cur_stop   : 当前生效止损 = 三者中最紧的一个（多取max / 空取min）

    实现说明：直接消费 `walk_fusion_states` 的最后一帧，与回测共享同一套循环（单一真源，无口径分叉）。
    """
    last = {
        "state": 0, "entry_i": -1, "entry_px": None, "first_px": None, "lots": 0,
        "entry_atr": None, "peak": None, "trough": None, "be_done": False,
        "be_trigger": None, "init_stop": None, "trail_stop": None, "cur_stop": None,
    }
    for d in walk_fusion_states(o, h, l, c, htf_dir, p):
        last = d
    return last


def _levels(state: int, entry_px: float, e_atr: float, peak: float, trough: float,
            be_done: bool, p) -> dict:
    """把持仓明细换算成实际价位：保本触发价 / 初始止损 / 吊灯止损 / 当前生效止损。

    注意 entry_px 此处传的是**加权均价**（加码后上移）。V3.4 起加码由浮盈门槛门控
    （结构保本约束停用），加码后 cur_stop 不再保证 ≥ 均价 —— 约 53% 加码单以亏损收场，
    但亏损中位仅 −0.16R、均值 +1.00R（PRD §17.2/§17.3 实测）。
    """
    if state == 0 or not (e_atr and e_atr > 0):
        return {"be_trigger": None, "init_stop": None,
                "trail_stop": None, "cur_stop": None}
    if state == 1:      # 多：止损在下，取最高
        init_stop = entry_px - p.sl_atr * e_atr
        trail_stop = (peak - p.trail_atr * e_atr) if p.trail_atr > 0 else None
        be_trigger = entry_px + p.be_r * e_atr if p.be_r > 0 else None
        cur = init_stop if trail_stop is None else max(init_stop, trail_stop)
        if be_done:
            cur = max(cur, entry_px)
    else:               # 空：止损在上，取最低
        init_stop = entry_px + p.sl_atr * e_atr
        trail_stop = (trough + p.trail_atr * e_atr) if p.trail_atr > 0 else None
        be_trigger = entry_px - p.be_r * e_atr if p.be_r > 0 else None
        cur = init_stop if trail_stop is None else min(init_stop, trail_stop)
        if be_done:
            cur = min(cur, entry_px)
    return {"be_trigger": be_trigger, "init_stop": init_stop,
            "trail_stop": trail_stop, "cur_stop": cur}


# -----------------------------------------------------
# 持久化：每品种当前持仓状态（状态机去重用）
# -----------------------------------------------------
class FusionPosition(Base):
    __tablename__ = "fusion_position"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[str] = mapped_column(Text, default="FLAT")  # FLAT / LONG / SHORT
    entry_price: Mapped[float | None] = mapped_column(Numeric(20, 4))
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # ---- 推送辅助（2026-09-15）：用于「吊灯止损上移才推」与「开仓信号复提防漏」----
    # 上次推送给用户的当前生效止损位（比它更靠有利方向才视为"上移"）
    last_stop: Mapped[float | None] = mapped_column(Numeric(20, 4))
    # 上次推送时是否已挂保本（False→True 是一次性事件，必须推）
    last_be_done: Mapped[bool] = mapped_column(Boolean, default=False)
    # 最近一次「开仓/反手」信号的时间（用于在 repeat 窗口内每轮复提，防用户漏看）
    signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 该品种最近一次被推送的时间（用于止损变动去重）
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ---- P1 加码（2026-09-20）：当前手数。较上一轮增加即为「加码事件」，需要推送 ----
    lots: Mapped[int] = mapped_column(Integer, default=1)


class FusionPushLog(Base):
    """推送留痕：每条推送落库，用户可随时回查"历史上发过哪些信号"。

    这是「怕错过信号」的第一道保险——即使消息被微信折叠/没看到，记录也不会丢。
    """
    __tablename__ = "fusion_push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    kind: Mapped[str] = mapped_column(Text, default="heartbeat")  # signal/presession/heartbeat/startup
    title: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    n_signals: Mapped[int] = mapped_column(Integer, default=0)
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(Boolean, default=True)  # 主通道是否发送成功
    via: Mapped[str] = mapped_column(Text, default="pushplus")      # 实际成功通道


class FusionSignalLog(Base):
    """信号明细落库 —— 每次扫描产生的**结构化**信号逐条留存，供回测/复盘使用。

    与 `fusion_push_log` 的区别：后者存"推送文本"（给人看），本表存"可计算字段"（给程序用）。
    落库为旁路：任何失败只告警，绝不阻塞扫描与推送。
    """
    __tablename__ = "fusion_signal_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 信号时间(UTC)
    trade_date: Mapped[str] = mapped_column(Text, default="")             # 本地交易日 YYYY-MM-DD
    source: Mapped[str] = mapped_column(Text, default="fusion_scan")      # fusion_scan / pipeline_daily
    kind: Mapped[str] = mapped_column(Text, default="signal")             # signal / heartbeat / presession
    symbol: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)      # 开多/开空/平多/反手...（中文动作）
    direction: Mapped[str | None] = mapped_column(Text)   # 目标状态 LONG/SHORT/FLAT
    prev_state: Mapped[str | None] = mapped_column(Text)  # 变化前状态
    price: Mapped[float | None] = mapped_column(Float)    # 触发时最新价
    entry: Mapped[float | None] = mapped_column(Float)    # 引擎入场价（新仓）
    prev_entry: Mapped[float | None] = mapped_column(Float)
    stop: Mapped[float | None] = mapped_column(Float)     # 当前吊灯止损
    be_trigger: Mapped[float | None] = mapped_column(Float)   # 保本触发价
    be_done: Mapped[bool | None] = mapped_column(Boolean)
    atr: Mapped[float | None] = mapped_column(Float)      # 入场 ATR
    risk_px: Mapped[float | None] = mapped_column(Float)  # 初始风险(价差)
    lots: Mapped[int | None] = mapped_column(Integer)     # 手数
    pnl: Mapped[float | None] = mapped_column(Float)      # 持仓浮盈率
    extra: Mapped[dict | None] = mapped_column(JSON)      # 其余字段兜底


# 新增列的兼容迁移语句（表已存在时 create_all 不会补列）
_MIGRATIONS = (
    "ALTER TABLE fusion_position ADD COLUMN IF NOT EXISTS last_stop NUMERIC(20, 4)",
    "ALTER TABLE fusion_position ADD COLUMN IF NOT EXISTS last_be_done BOOLEAN DEFAULT FALSE",
    "ALTER TABLE fusion_position ADD COLUMN IF NOT EXISTS signal_at TIMESTAMPTZ",
    "ALTER TABLE fusion_position ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ",
    "ALTER TABLE fusion_position ADD COLUMN IF NOT EXISTS lots INTEGER DEFAULT 1",
)


def ensure_fusion_table(engine) -> None:
    """运行时建表（容器已启动、不重跑 init SQL，故用 CREATE IF NOT EXISTS 等价机制）。"""
    # 仅建本模块新增的表，不影响既有表
    Base.metadata.create_all(
        engine, tables=[FusionPosition.__table__, FusionPushLog.__table__,
                        FusionSignalLog.__table__]
    )
    # 老库补列（幂等）
    with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            try:
                conn.execute(text(stmt))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[fusion] migration skipped: {stmt[:60]}... ({e})")
    logger.info("[fusion] ensure fusion_position / fusion_push_log / fusion_signal_log table ok")


def persist_signals(session, signals: list[dict], ts_local: datetime,
                    source: str = "fusion_scan", kind: str = "signal",
                    prev_states: dict[str, str] | None = None) -> int:
    """把一轮扫描的信号结构化落库（旁路，失败只告警）。返回落库条数。

    ts_local 为带时区的本地时间；库内统一存 UTC。
    """
    if not signals:
        return 0
    prev_states = prev_states or {}
    utc = ts_local.astimezone(timezone.utc)
    trade_date = ts_local.strftime("%Y-%m-%d")
    rows = []
    for sg in signals:
        lv = sg.get("levels") or {}
        rows.append(FusionSignalLog(
            signal_at=utc,
            trade_date=trade_date,
            source=source,
            kind=kind,
            symbol=sg.get("symbol") or "",
            name=sg.get("name"),
            action=sg.get("action"),
            direction=sg.get("to"),
            prev_state=prev_states.get(sg.get("symbol")),
            price=_f(sg.get("close")),
            entry=_f(sg.get("new_entry") or sg.get("entry")),
            prev_entry=_f(sg.get("entry")),
            stop=_f(lv.get("stop")),
            be_trigger=_f(lv.get("be_trigger")),
            be_done=lv.get("be_done") if lv else None,
            atr=_f(lv.get("atr")),
            risk_px=_f(lv.get("risk_px")),
            lots=sg.get("lots"),
            pnl=_f(sg.get("pnl")),
            extra={k: v for k, v in sg.items()
                   if k not in ("symbol", "name", "action", "to", "close",
                                "entry", "new_entry", "levels", "pnl")} or None,
        ))
    try:
        session.bulk_save_objects(rows)
        session.flush()
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fusion] 信号落库失败（不影响推送）: {e}")
        return 0


def _f(v) -> float | None:
    """安全转 float（None/NaN/异常一律返回 None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


# -----------------------------------------------------
# 数据读取：从 hourly_bar 取最近 N 根小时线
# -----------------------------------------------------
def _read_src(session, symbol: str, limit: int, src: str) -> pd.DataFrame | None:
    """按单一 src 读取某品种小时线，返回按时间升序的 DataFrame。"""
    rows = (
        session.execute(
            select(
                HourlyBar.trade_datetime,
                HourlyBar.open,
                HourlyBar.high,
                HourlyBar.low,
                HourlyBar.close,
            )
            .where(HourlyBar.symbol == symbol)
            .where(HourlyBar.src == src)
            .order_by(HourlyBar.trade_datetime.desc())
            .limit(limit)
        )
        .all()
    )
    rows = list(reversed(rows))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def read_hourly_bars(session, symbol: str, limit: int) -> pd.DataFrame | None:
    """读取某品种小时线。

    关键修复（2026-09-15）：按 src 过滤，引擎只消费**单一口径**的小时线，
    杜绝 akshare(同花顺/结束时刻标签) 与 tqsdk/CSV(起点标签) 两套口径被当成两根
    不同的 K 同时喂进 EMA/ATR，导致周期参数被砍半、信号与回测/图表口径不一致
    （实测 09-14 单日读进 11 根 K，而真实只有 6 根）。

    口径由 fusion.hourly_src 控制（默认 akshare，即 akshare 新浪主连分时 futures_zh_minute_sina，对应图表「收盘时刻」标签的 K）。
    仅当主源根数不足 min_bars（数据源故障期）才回退到另一源，并在日志告警——
    正常生产应恒为单源；故障期混用是无奈兜底，非预期状态。
    """
    from app.core.config import get_settings
    cfg = get_settings().fusion
    src = cfg.hourly_src
    df = _read_src(session, symbol, limit, src)
    if df is None or len(df) < cfg.min_bars:
        other = "tqsdk" if src == "akshare" else "akshare"
        logger.warning(
            f"[fusion] 主源 src={src} 仅 {len(df) if df is not None else 0} 根"
            f"(<{cfg.min_bars})，回退 src={other}（故障期兜底，正常应恒为单源）"
        )
        df = _read_src(session, symbol, limit, other)
    return df


def drop_forming_bars(df: pd.DataFrame | None, now: datetime | None = None) -> pd.DataFrame | None:
    """剔除「尚未收盘」的小时K。

    背景（2026-09-14 实测确认）：hourly_bar.trade_datetime 是该K的**收盘时刻**（北京时间整点），
    采集源会把「正在形成、尚未收盘」的那一根也写进来。于是：
      - 同一小时内、没有任何新K，信号的 state 也会自己变（实测 50 品种里有 2 个翻转）；
      - 有的品种的持仓状态**完全依赖**这根未收盘K（去掉它就从"持多"变回"空仓"）。
    这与文档/回测「只关心最后一根已收盘小时K、close-only、无前视」的口径直接冲突，
    对按推送下单的人是硬伤：信号会在下一轮消失或反手。

    判定：now < dt 即该K尚未收盘（dt 是收盘时刻），丢弃。
    """
    if df is None or df.empty:
        return df
    now = now or datetime.now(_SH_TZ)
    s = pd.to_datetime(df["dt"])
    # 统一到上海时区（与 DB 存储口径一致），dt 即收盘时刻
    if s.dt.tz is None:
        s = s.dt.tz_localize(_SH_TZ)
    else:
        s = s.dt.tz_convert(_SH_TZ)
    keep = (s <= pd.Timestamp(now)).to_numpy()
    if keep.all():
        return df
    n_drop = int((~keep).sum())
    # 注意：本项目 logger 是 loguru，只认 {} / f-string，用 %s 位置参数会原样打印。
    # 未收盘K是常态（采集器每轮都会写进一根正在形成的K），故降为 debug 免刷屏。
    logger.debug(
        f"[fusion] 剔除未收盘K {n_drop} 根（最新 dt={s.iloc[-1]} 晚于 now={now:%Y-%m-%d %H:%M}）"
    )
    return df[keep].reset_index(drop=True)


def drop_unsettled(df, settle_delay_sec: int, now=None):
    """剔除「刚收盘、可能尚未定稿」的那根K。

    背景（2026-09-15 JD 实战）：小时K在收盘瞬间数据源（新浪分钟线）返回的 close
    是过渡值，1~2 分钟后才改写为定稿值（JD 09:00-10:00 根实测 3814→3810）。引擎若在
    定稿前就据此出信号，会出现「10:00 空 @3814 → 10:30 多 @3810」这种同源数据漂移导致的
    反复反手。这里把「收盘时刻距现在不足 settle_delay_sec 秒」的最新一根也剔除，
    引擎状态因此对扫描时刻稳定，不再自我翻面。
    """
    if df is None or df.empty or not settle_delay_sec or settle_delay_sec <= 0:
        return df
    now = now or datetime.now(_SH_TZ)
    last = pd.to_datetime(df["dt"].iloc[-1])
    if last.tzinfo is None:
        last = last.tz_localize(_SH_TZ)
    else:
        last = last.tz_convert(_SH_TZ)
    if (pd.Timestamp(now) - last).total_seconds() < settle_delay_sec:
        return df.iloc[:-1].reset_index(drop=True)
    return df


# -----------------------------------------------------
# 评估单品种 / 全品种
# -----------------------------------------------------
_POS = {0: "FLAT", 1: "LONG", 2: "SHORT"}


def evaluate_symbol(session, symbol: str, p) -> dict:
    df = read_hourly_bars(session, symbol, p.max_bars)
    if getattr(p, "closed_bars_only", True):
        df = drop_forming_bars(df)
    # 定稿守卫：剔除刚收盘、数据源可能还没定稿的最新一根，避免同源漂移导致反复反手
    df = drop_unsettled(df, getattr(p, "settle_delay_sec", 0))
    if df is None or len(df) < p.min_bars:
        return {
            "symbol": symbol,
            "state": 0,
            "bars": 0 if df is None else len(df),
            "error": "bars<min" if df is None else f"bars={len(df)}<{p.min_bars}",
        }
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    ema140 = ema(c, p.ema_k)
    dir_raw = np.where(c > ema140, 1, -1)
    htf_dir = np.concatenate([[0], dir_raw[:-1]])  # 用前一根方向门控（对齐已收盘，无前视）
    det = fusion_state_detail(o, h, l, c, htf_dir, p)
    st = det["state"]
    latest_dt = df["dt"].iloc[-1]
    return {
        "symbol": symbol,
        "state": st,
        "latest_dt": latest_dt,
        "latest_close": float(c[-1]),
        "bars": len(df),
        # 持仓明细（供推送给出止损位/保本位）
        "entry_px": det["entry_px"],
        "first_px": det["first_px"],
        "lots": det["lots"],
        "entry_atr": det["entry_atr"],
        "entry_dt": (df["dt"].iloc[det["entry_i"]] if det["entry_i"] >= 0 else None),
        "be_done": det["be_done"],
        "be_trigger": det["be_trigger"],
        "init_stop": det["init_stop"],
        "trail_stop": det["trail_stop"],
        "cur_stop": det["cur_stop"],
        "error": None,
    }


def evaluate_all(session, specs, p) -> list[dict]:
    out = []
    for spec in specs:
        try:
            out.append(evaluate_symbol(session, spec.symbol, p))
        except Exception as e:  # noqa: BLE001
            out.append({"symbol": spec.symbol, "state": 0, "error": str(e)[:200]})
    return out


# -----------------------------------------------------
# 持仓状态读写
# -----------------------------------------------------
def get_position(session, symbol: str) -> str:
    obj = session.get(FusionPosition, symbol)
    return obj.position if obj else "FLAT"


def _get_or_create_position(session, symbol: str) -> FusionPosition:
    """get-or-create FusionPosition。

    ⚠ 2026-09-23 修复：本项目 session 为 autoflush=False，upsert_position 新建的
    pending 行对随后的 session.get 不可见 → set_push_state 会再建一个同主键对象，
    flush 时 UniqueViolation（清空基线后的冷启动首次暴露）。故先扫 session.new。
    """
    for pending in session.new:
        if isinstance(pending, FusionPosition) and pending.symbol == symbol:
            return pending
    obj = session.get(FusionPosition, symbol)
    if obj is None:
        obj = FusionPosition(symbol=symbol, position="FLAT")
        session.add(obj)
    return obj


def upsert_position(session, symbol: str, position: str, entry_price=None, entry_at=None,
                    lots=None) -> None:
    obj = _get_or_create_position(session, symbol)
    obj.position = position
    obj.entry_price = entry_price
    obj.entry_at = entry_at
    if lots is not None:
        obj.lots = int(lots)
    obj.updated_at = datetime.now(timezone.utc)
    session.add(obj)


_UNSET = object()


def set_push_state(session, symbol: str, *, last_stop=_UNSET, last_be_done=_UNSET,
                   signal_at=_UNSET, pushed_at=_UNSET, lots=_UNSET) -> FusionPosition:
    """更新推送辅助字段（只有显式传入的才覆盖；可显式传 None 来清除）。"""
    obj = _get_or_create_position(session, symbol)
    for field, val in (
        ("last_stop", last_stop),
        ("last_be_done", last_be_done),
        ("signal_at", signal_at),
        ("pushed_at", pushed_at),
        ("lots", lots),
    ):
        if val is not _UNSET:
            setattr(obj, field, val)
    obj.updated_at = datetime.now(timezone.utc)
    return obj


__all__ = [
    "FusionPosition",
    "FusionPushLog",
    "ensure_fusion_table",
    "read_hourly_bars",
    "drop_forming_bars",
    "evaluate_symbol",
    "evaluate_all",
    "fusion_state",
    "fusion_state_detail",
    "walk_fusion_states",
    "get_position",
    "upsert_position",
    "set_push_state",
    "POS_MAP",
]

POS_MAP = _POS
