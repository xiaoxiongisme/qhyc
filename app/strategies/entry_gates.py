# -*- coding: utf-8 -*-
"""入场门控 C_final_v2（2026-11-01 信号级 + 引擎级双验证通过，转入影子观察）。

规则（两条，全部用**前一交易日**收盘值，无前视；只拦新开仓，离场/保本/吊灯/加码不动）：
  ① K45 中段停：日线 K=45 摆动段趋势进度 ∈ [33%, 50%) 时禁止一切新开仓（多空双侧）；
  ② 逆 regime 空头停：50 主连等权日收益指数 > SMA250（前一交易日）时停发空头新开仓。

验证结论（50 品种全历史，IS=2015-2020 / OOS=2021-2026）：
  * 信号级：IS +538→+671R(+25%)、OOS +54→+297R(+449%)，安慰剂 300 次随机砍同量
    双侧 0% 显著；
  * 引擎级（真实成本+乘数）：IS +546k→+875k(+60%)、OOS −735k→+298k（翻转 +103 万）；
  * 保留意见：剔 TOP3 后 OOS 仍为负（−32.5 万，但边际改善 +141 万不依赖单一品种）；
    2022 年（无趋势震荡）门控略损 1.5 万。

口径纪律（改动须三处同步：本模块 / scripts/validate_entry_gate.py /
scripts/entry_filter_study2.py —— 单一真源是本模块，另两处为研究复刻）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text

K45 = 45          # 摆动确认窗口（日线根数）
SMA_N = 250       # 市场 regime 年线（交易日）
MID_LO, MID_HI = 0.33, 0.50   # 中段进度带 [33%, 50%)
# final_v3：仅豁免「逆 regime 空头停」，K45 中段停仍对这些品种生效。
# 名单只用 2021-2023 选取，2024-2026 前向验证 +32.9R；当前仅用于影子观察。
FINAL_V3_REGIME_SHORT_WHITELIST = frozenset({"LH888", "LC888", "B888", "AG888", "L888"})
ENTRY_GATE_PROFILES = frozenset({"final_v2", "final_v3"})


# ---------------------------------------------------------------
# ① K45 日线摆动状态（与 validate_entry_gate.k45_daily_state 逐位一致）
# ---------------------------------------------------------------
def k45_daily_state(dates, h, l, c, K: int = K45) -> pd.DataFrame:
    """日线摆动段方向 + 趋势进度。

    摆动点定义：以 K 根为半径的窗口极值（H/L 各自取首次出现位置）；
    相邻同类型候选合并保留更极端者；段方向 = 最近一个已确认（i + K <= t）摆动点；
    进度 prog = 段起点到当前收盘的位置占比（L 起点：0=谷 1=创新高；H 起点：0=峰 1=创新低）。
    调用方负责 .shift(1)（前一日口径）。
    """
    n = len(c)
    cand = []
    for i in range(n):
        lo, hi = max(0, i - K), min(n, i + K + 1)
        wh = h[lo:hi].max()
        if h[i] == wh and lo + int(np.argmax(h[lo:hi])) == i:
            cand.append((i, "H", float(h[i])))
        wl = l[lo:hi].min()
        if l[i] == wl and lo + int(np.argmin(l[lo:hi])) == i:
            cand.append((i, "L", float(l[i])))
    cand.sort(key=lambda x: x[0])
    clean = []
    for i, tp, px in cand:
        if clean and clean[-1][1] == tp:
            if (tp == "H" and px > clean[-1][2]) or (tp == "L" and px < clean[-1][2]):
                clean[-1] = (i, tp, px)
        else:
            clean.append((i, tp, px))
    seg_dir = np.zeros(n, dtype=int)
    prog = np.full(n, np.nan)
    cur = None
    j = 0
    for t in range(n):
        while j < len(clean) and clean[j][0] + K <= t:
            cur = clean[j]
            j += 1
        if cur is None:
            continue
        idx, tp, px = cur
        if tp == "L":
            seg_dir[t] = 1
            runmax = h[idx:t + 1].max()
            denom = runmax - px
            prog[t] = (c[t] - px) / denom if denom > 0 else np.nan
        else:
            seg_dir[t] = -1
            runmin = l[idx:t + 1].min()
            denom = px - runmin
            prog[t] = (px - c[t]) / denom if denom > 0 else np.nan
    return pd.DataFrame({"seg_dir": seg_dir, "prog": prog}, index=dates)


# ---------------------------------------------------------------
# ② 市场 regime（50 主连等权指数 vs SMA250）
# ---------------------------------------------------------------
def market_regime_series(session, symbols: list[str]) -> pd.Series | None:
    """等权日收益累积指数 > SMA250 的布尔序列（已 shift(1) = 前一日口径）。

    与验证脚本同口径：close 的 pct_change 等权平均 → (1+r).cumprod() →
    rolling(SMA_N).mean() → 上穿布尔 → shift(1)。数据不足 250 日时返回 None
    （门控退化为「只受 K45 约束」，与验证期 2015 前的行为一致）。
    """
    if not symbols:
        return None
    rows = session.execute(text(
        "SELECT symbol, trade_date, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY trade_date"), {"syms": list(symbols)}).all()
    if not rows:
        return None
    dd = pd.DataFrame(rows, columns=["symbol", "dt", "close"])
    dd["dt"] = pd.to_datetime(dd["dt"])
    wide = dd.pivot_table(index="dt", columns="symbol", values="close")
    rets = wide.pct_change(fill_method=None)
    idx_eq = (1.0 + rets.mean(axis=1, skipna=True)).cumprod()
    sma = idx_eq.rolling(SMA_N, min_periods=SMA_N).mean()
    regime_up = (idx_eq > sma).astype(float)
    regime_up[~(idx_eq.notna() & sma.notna())] = np.nan
    reg_series = pd.Series(regime_up.values, index=idx_eq.index).shift(1)
    reg_series.index = pd.to_datetime(reg_series.index)
    return reg_series


# ---------------------------------------------------------------
# 对小时线构造 (long_ok, short_ok) 门控数组
# ---------------------------------------------------------------
def build_entry_gates(hourly_dts: np.ndarray, st45: pd.DataFrame | None,
                      reg_series: pd.Series | None, *,
                      regime_short_enabled: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """逐位复刻 validate_entry_gate.py 的 gate 构造（小时 bar → 前一日日线状态）。

    hourly_dts: 小时线 dt 列（tz-aware 或 naive 均可，取 .date() 前先统一）；
    st45: 该品种 K45 状态（**已 shift(1)**，index=交易日）；
    reg_series: 市场 regime（**已 shift(1)**，index=交易日）。
    返回 (long_ok, short_ok)，int8，1=允许。默认 1；两规则只置 0。
    """
    n = len(hourly_dts)
    progs = np.full(n, np.nan)
    if st45 is not None and len(st45):
        sdates = st45.index
        for i in range(n):
            d = pd.Timestamp(hourly_dts[i]).date()
            pos = sdates.searchsorted(pd.Timestamp(d), side="left")
            if pos > 0 and sdates[pos - 1].date() < d:
                progs[i] = st45.iloc[pos - 1]["prog"]
    mid = np.isfinite(progs) & (progs >= MID_LO) & (progs < MID_HI)
    gate_l = (~mid).astype(np.int8)
    gate_s = gate_l.copy()
    if regime_short_enabled and reg_series is not None and len(reg_series):
        reg_dates = reg_series.index.values
        for i in range(n):
            if mid[i]:
                continue
            d = pd.Timestamp(hourly_dts[i]).date()
            pos = np.searchsorted(reg_dates, np.datetime64(pd.Timestamp(d)), side="left")
            while pos > 0 and pd.Timestamp(reg_dates[pos - 1]).date() >= d:
                pos -= 1
            if pos <= 0:
                continue
            v = reg_series.iloc[pos - 1]
            if np.isfinite(v) and v > 0.5:  # 指数在年线上方 → 停发空头
                gate_s[i] = 0
    return gate_l, gate_s


# ---------------------------------------------------------------
# 缓存（生产每 15 分钟扫一轮，日线级状态一天至多变一次）
# ---------------------------------------------------------------
_k45_cache: dict[str, tuple[str, pd.DataFrame]] = {}   # symbol -> (末根交易日, st[shift1])
_regime_cache: dict[str, pd.Series | None] = {}        # 末根交易日 -> series


def _last_daily_date(session, symbol: str | None) -> str:
    if symbol:
        row = session.execute(text(
            "SELECT max(trade_date) FROM l0_raw.daily_bar WHERE symbol = :s"),
            {"s": symbol}).scalar()
    else:
        row = session.execute(text(
            "SELECT max(trade_date) FROM l0_raw.daily_bar")).scalar()
    return str(row) if row is not None else ""


def get_k45_state(session, symbol: str) -> pd.DataFrame | None:
    """带日级缓存的单品种 K45 状态（已 shift(1)）。日线无数据返回 None。"""
    key = _last_daily_date(session, symbol)
    if not key:
        return None
    hit = _k45_cache.get(symbol)
    if hit and hit[0] == key:
        return hit[1]
    rows = session.execute(text(
        "SELECT trade_date, high, low, close FROM l0_raw.daily_bar "
        "WHERE symbol = :s ORDER BY trade_date"), {"s": symbol}).all()
    if not rows:
        return None
    g = pd.DataFrame(rows, columns=["dt", "high", "low", "close"])
    st = k45_daily_state(g["dt"].values,
                         g["high"].to_numpy(float),
                         g["low"].to_numpy(float),
                         g["close"].to_numpy(float)).shift(1)
    st.index = pd.to_datetime(st.index)
    _k45_cache[symbol] = (key, st)
    return st


def get_market_regime(session, symbols: list[str]) -> pd.Series | None:
    """带日级缓存的市场 regime（已 shift(1)）。"""
    key = _last_daily_date(session, None)
    if not key:
        return None
    if key in _regime_cache:
        return _regime_cache[key]
    s = market_regime_series(session, symbols)
    _regime_cache.clear()          # 只留最新一天，避免长期运行内存膨胀
    _regime_cache[key] = s
    return s


def gates_for_symbol(session, symbol: str, hourly_df: pd.DataFrame,
                     symbols: list[str] | None = None,
                     profile: str = "final_v2") -> tuple[np.ndarray, np.ndarray]:
    """对外主入口：给某品种的小时线构造入场门控数组。

    final_v2：K45 中段停 + 全品种逆 regime 空头停；
    final_v3：同 v2，但五个前向验证品种豁免逆 regime 空头停（K45 不豁免）。
    """
    if profile not in ENTRY_GATE_PROFILES:
        raise ValueError(f"未知 entry_gate_profile: {profile}")
    if symbols is None:
        from app.core.config import get_settings
        symbols = [sp.symbol for sp in get_settings().main_contracts]
    st45 = get_k45_state(session, symbol)
    reg = get_market_regime(session, symbols)
    regime_short_enabled = not (
        profile == "final_v3" and symbol in FINAL_V3_REGIME_SHORT_WHITELIST)
    return build_entry_gates(hourly_df["dt"].to_numpy(), st45, reg,
                             regime_short_enabled=regime_short_enabled)
