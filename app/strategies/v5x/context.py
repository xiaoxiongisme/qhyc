# -*- coding: utf-8 -*-
"""V5X.3 DailyCtx 数据供给 — CL-20261108-22 / C2。

V5X 的 ``SymbolEngine`` 只认 ``DailyCtx``，不自己查库。本模块负责把 qhyc 的
K 线与基本面数据翻译成 ``DailyCtx``，并明确标注每一项的口径来源。

五项上下文
----------
| 字段 | 口径 | 前视 |
|---|---|---|
| ``hvol`` | 昨日日振幅 > 该品种 250 日 80 分位（min_periods=60，**shift(1)**） | 无 |
| ``member_z`` | top20 净持仓（多−空）的**时序 z**，60 期窗，**lag=1 日** | 无 |
| ``blocked`` | 簇等权日对数收益绝对值 > 3σ(250d) | 无（当日判定，当日封禁） |
| ``sess_last_hours`` | 每个交易日最后一根 bar 的小时集合 | 无 |
| ``regime`` / ``vol_z`` | 日已实现波动（60m 对数收益 std）分位分层 | **frozen 模式含前视**，见下 |

★ 两个必须知道的坑
------------------
1. **member_z 不是 `factor_value.member_net_z`**：那是 ``groupby(trade_date)``
   的横截面 z（品种间排序），当单品种择时门用会 100% 误杀（CL-20261108-20）。
   而且它的 raw 是 **top5** 净持仓，V5X 的 M5 门要的是 **top20**。本模块直接
   从 ``member_position_rank_summary`` 自算 top20 时序 z，不复用该因子。
   （`member_net_ts_z` 解决的是「z 类型」；top5/top20 是另一层口径差异。）

2. **regime 的 frozen 标定含前视**：V5X 的 ``regime_table.csv`` 用**全样本**
   分位数（q33/q66）与全样本均值/std 计算 vol_z，并且用**当日**已实现波动
   判定当日体制。要点是它只影响「极端高波禁开仓」这条尾部门控，不参与方向，
   但严格来说仍是样本内信息。故本模块提供两种模式：
     · ``"frozen"``（默认）= 复现 V5X.3 冻结口径，**报告必须标注含前视**；
     · ``"strict"`` = expanding 分位 + 前一日 regime，无前视，供稳健性对照。

交易日归属
----------
V5X 的 ``td_assign``：夜盘（``hour >= 18``）归**次一交易日**。qhyc 的 bar bucket
是收盘时刻（夜盘 21:00/23:00/次日凌晨），故本模块用 ``trade_calendar`` 的
开市日序列做 ``searchsorted``，而不是简单 +1 天（否则周五夜盘会落到周六）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.data.barstore import variety_spec
from app.data.cost import fee_per_lot
from app.strategies.v5x.engine import CLUSTER, CostCfg, DailyCtx

__all__ = ["V5XMarketData", "load_market_data", "build_ctx", "build_cost"]

#: 夜盘起点小时：>= 该小时的 bar 归次一交易日（与 V5X td_assign 一致）。
NIGHT_START_HOUR = 18
#: hvol：日振幅 250 日分位
HVOL_Q = 0.80
HVOL_WIN = 250
HVOL_MIN = 60
#: 簇冲击：|簇等权日收益| > K σ(250d)
CLUSTER_SHOCK_K = 3.0
CLUSTER_SHOCK_WIN = 250
CLUSTER_SHOCK_MIN = 60
#: M5 会员净持仓时序 z
MEMBER_Z_TOPN = 20          # ★ top20，不是 factor_value 的 top5
MEMBER_Z_WIN = 60
MEMBER_Z_MIN = 20


@dataclass
class V5XMarketData:
    """一次装载、多品种复用的市场数据容器。"""

    src: str
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    hvol: dict[str, dict[Any, bool]] = field(default_factory=dict)
    blocked: dict[str, set] = field(default_factory=dict)
    member_z: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    regime: dict[tuple[str, Any], tuple[int, float]] = field(default_factory=dict)
    sess_last_hours: dict[str, frozenset] = field(default_factory=dict)
    open_days: np.ndarray = field(default_factory=lambda: np.array([], dtype="datetime64[D]"))
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 交易日历
def load_open_days(session, start: date, end: date) -> np.ndarray:
    """取开市日序列（``datetime64[D]``），用于夜盘归属。"""
    rows = session.execute(text("""
        SELECT DISTINCT trade_date FROM public.trade_calendar
        WHERE is_open AND trade_date >= :a AND trade_date <= :b
        ORDER BY trade_date
    """), {"a": start - timedelta(days=30), "b": end + timedelta(days=30)}).all()
    if not rows:
        return np.array([], dtype="datetime64[D]")
    return np.array([np.datetime64(r[0], "D") for r in rows])


def assign_td(buckets: pd.Series, open_days: np.ndarray) -> np.ndarray:
    """把 bar 的收盘时刻映射到交易日（夜盘 >=18:00 归下一交易日）。

    日历缺失（空数组）时退化为「当日」，但会把这一事实写进 diagnostics ——
    静默退化会让夜盘 bar 归错日，hvol / regime 全部错位而不报错。
    """
    ts = pd.to_datetime(buckets)
    d = ts.dt.date.to_numpy()
    h = ts.dt.hour.to_numpy()
    day = np.array([np.datetime64(x, "D") for x in d])
    if not len(open_days):
        return day
    need = day + np.timedelta64(1, "D")
    idx = np.searchsorted(open_days, need, side="left")
    idx = np.clip(idx, 0, len(open_days) - 1)
    nxt = open_days[idx]
    return np.where(h >= NIGHT_START_HOUR, nxt, day)


# ---------------------------------------------------------------- 取数
_SQL_BA = text("""
SELECT b.symbol, b.bucket, b.open, b.high, b.low, b.close, b.volume,
       r.close AS raw_c
FROM l2_adj.bar_60m_ba b
JOIN l1_mkt.bar_60m r ON r.symbol = b.symbol AND r.bucket = b.bucket
WHERE b.symbol = ANY(:syms) AND b.bucket >= :a AND b.bucket < :b
ORDER BY b.symbol, b.bucket
""")

_SQL_RAW = text("""
SELECT symbol, bucket, open, high, low, close, volume, close AS raw_c
FROM l1_mkt.bar_60m
WHERE symbol = ANY(:syms) AND bucket >= :a AND bucket < :b
ORDER BY symbol, bucket
""")


#: 单次 SQL 的品种批大小。生产库 ``/dev/shm`` 只有 64MB（docker 默认），
#: 38 品种 × 12 年一次 JOIN 会把并行 worker 的 shm 段撑爆：
#: ``could not resize shared memory segment ... No space left on device``。
#: 分批 + 关并行是最小代价的解法（实测 2026-11-08）。
_BAR_BATCH = 8


def _load_bars(session, symbols: list[str], start: date, end: date,
               src: str) -> pd.DataFrame:
    sql = _SQL_BA if src in ("bar_60m_ba", "ba", "back_adj") else _SQL_RAW
    cols = ["symbol", "bucket", "open", "high", "low", "close", "volume", "raw_c"]
    try:
        # SET LOCAL 只影响当前事务；并行 worker 的 shm 申请是这次 DiskFull 的真凶
        session.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
    except Exception:                                        # noqa: BLE001
        pass                                                 # 只读副本上可能无权
    frames: list[pd.DataFrame] = []
    for i in range(0, len(symbols), _BAR_BATCH):
        chunk = list(symbols[i:i + _BAR_BATCH])
        rows = session.execute(sql, {"syms": chunk, "a": start,
                                     "b": end}).fetchall()
        if rows:
            frames.append(pd.DataFrame(rows, columns=cols))
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True)
    for c in ("open", "high", "low", "close", "volume", "raw_c"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


# ---------------------------------------------------------------- 日线派生
def _daily_frame(bars: pd.DataFrame, open_days: np.ndarray) -> pd.DataFrame:
    """按 (symbol, 交易日) 聚合出日振幅与日已实现波动。"""
    df = bars[["symbol", "bucket", "high", "low", "close"]].copy()
    df["td"] = assign_td(df["bucket"], open_days)
    g = df.groupby(["symbol", "td"], sort=True)
    out = g.agg(hh=("high", "max"), ll=("low", "min"),
                c=("close", "last")).reset_index()
    out["amp"] = (out["hh"] - out["ll"]) / out["c"].replace(0, np.nan)
    # 日已实现波动 = 当日 60m 对数收益 std（V5X v5x_regime._daily_vol 同口径）
    lr = df.assign(lr=np.log(df["close"] / g["close"].shift(1)))[["symbol", "td", "lr"]]
    vol = lr.groupby(["symbol", "td"], sort=True)["lr"].std().reset_index(name="vol")
    return out.merge(vol, on=["symbol", "td"], how="left")


def _hvol_flags(daily: pd.DataFrame) -> dict[str, dict[Any, bool]]:
    """昨日振幅 > 该品种 250 日 80 分位 → 当日禁开仓（shift(1)，无前视）。"""
    out: dict[str, dict[Any, bool]] = {}
    for sym, g in daily.groupby("symbol", sort=False):
        g = g.sort_values("td")
        q = g["amp"].rolling(HVOL_WIN, min_periods=HVOL_MIN).quantile(HVOL_Q).shift(1)
        flag = (g["amp"] > q).fillna(False)
        out[sym] = dict(zip(g["td"], flag.to_numpy(bool)))
    return out


def _cluster_shock(daily: pd.DataFrame) -> dict[str, set]:
    """簇等权日对数收益 |·| > 3σ(250d) → 该簇当日封禁。"""
    g = daily.sort_values(["symbol", "td"]).copy()
    g["lret"] = np.log(g["c"] / g.groupby("symbol")["c"].shift(1))
    g["cl"] = g["symbol"].map(
        lambda x: CLUSTER.get(x[:-3] if x.endswith("888") else x))
    out: dict[str, set] = {}
    for cl, sub in g.dropna(subset=["cl"]).groupby("cl"):
        rr = sub.pivot_table(index="td", columns="symbol", values="lret").mean(axis=1)
        sd = rr.rolling(CLUSTER_SHOCK_WIN, min_periods=CLUSTER_SHOCK_MIN).std()
        flag = (rr.abs() > CLUSTER_SHOCK_K * sd).fillna(False)
        out[str(cl)] = set(rr.index[flag.to_numpy(bool)])
    return out


def _regime_table(daily: pd.DataFrame, mode: str) -> dict[tuple[str, Any], tuple[int, float]]:
    """波动体制：0=低波 1=中波 2=高波；vol_z 为相对该品种自身的 z。

    mode="frozen"：全样本 33/66 分位 + 全样本 mean/std（复现 V5X.3，**含前视**）；
    mode="strict"：expanding 分位 + expanding mean/std，且一律 shift(1)（无前视）。
    """
    if mode not in ("frozen", "strict"):
        raise ValueError(f"未知 regime_mode={mode!r}（只支持 frozen / strict）")
    out: dict[tuple[str, Any], tuple[int, float]] = {}
    for sym, g in daily.groupby("symbol", sort=False):
        g = g.sort_values("td")
        v = g["vol"].astype(float)
        if mode == "frozen":
            q33 = float(np.nanpercentile(v.dropna(), 33)) if v.notna().sum() >= 30 else np.nan
            q66 = float(np.nanpercentile(v.dropna(), 66)) if v.notna().sum() >= 30 else np.nan
            mu, sd = v.mean(), v.std(ddof=0)
            z = (v - mu) / (sd if sd else np.nan)
        else:
            q33 = v.expanding(min_periods=30).quantile(0.33).shift(1)
            q66 = v.expanding(min_periods=30).quantile(0.66).shift(1)
            mu = v.expanding(min_periods=30).mean().shift(1)
            sd = v.expanding(min_periods=30).std(ddof=0).shift(1)
            z = (v - mu) / sd.replace(0, np.nan)
        for i, (td, vol) in enumerate(zip(g["td"], v.to_numpy(float))):
            _q33 = q33.iloc[i] if hasattr(q33, "iloc") else q33
            _q66 = q66.iloc[i] if hasattr(q66, "iloc") else q66
            if not np.isfinite(vol) or not np.isfinite(_q33) or not np.isfinite(_q66):
                rg = 1
            elif vol <= _q33:
                rg = 0
            elif vol >= _q66:
                rg = 2
            else:
                rg = 1
            _z = float(z.iloc[i]) if hasattr(z, "iloc") else float(z)
            out[(sym, td)] = (int(rg), _z if np.isfinite(_z) else 0.0)
    return out


def _sess_last_hours(bars: pd.DataFrame, open_days: np.ndarray) -> dict[str, frozenset]:
    """每个交易日**最后一根** bar 的小时集合（P6 会话末根禁开仓用）。"""
    df = bars[["symbol", "bucket"]].copy()
    df["td"] = assign_td(df["bucket"], open_days)
    df["hour"] = pd.to_datetime(df["bucket"]).dt.hour
    last = df.groupby(["symbol", "td"], sort=True)["hour"].last().reset_index()
    out: dict[str, frozenset] = {}
    for sym, g in last.groupby("symbol", sort=False):
        out[sym] = frozenset(int(x) for x in g["hour"].unique())
    return out


# ---------------------------------------------------------------- M5 会员 z
def _member_ts_z(session, symbols: list[str], start: date, end: date
                 ) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """top20 净持仓的**时序 z**（严格 lag=1 日，与 V5X M5 门同口径）。

    ⚠ 不复用 ``factor_value.member_net_z``：那是横截面 z 且 raw 用 top5。
    """
    rows = session.execute(text("""
        SELECT symbol, report_date,
               long_open_interest_top20 - short_open_interest_top20 AS net
        FROM l1_mkt.member_position_rank_summary
        WHERE symbol = ANY(:syms) AND report_date >= :a AND report_date <= :b
        ORDER BY symbol, report_date
    """), {"syms": list(symbols),
           "a": start - timedelta(days=400), "b": end}).fetchall()
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["symbol", "report_date", "net"])
    df["net"] = pd.to_numeric(df["net"], errors="coerce").astype(float)
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("report_date")
        if len(g) < MEMBER_Z_WIN + 10:
            continue
        ser = pd.Series(g["net"].to_numpy(float))
        mu = ser.rolling(MEMBER_Z_WIN, min_periods=MEMBER_Z_MIN).mean().shift(1)
        sd = ser.rolling(MEMBER_Z_WIN, min_periods=MEMBER_Z_MIN).std(ddof=0).shift(1)
        z = ((ser - mu) / sd.replace(0, np.nan)).to_numpy(float)
        td = np.array([np.datetime64(x, "D") for x in g["report_date"]])
        out[str(sym)] = (td, z)
    return out


# ---------------------------------------------------------------- 成本
def build_cost(symbol: str, on_date: date) -> CostCfg:
    """按 qhyc ``dim_trading_cost`` 的真实费率构造 V5X ``CostCfg``。

    与 ``fusion_backtest`` 同一费率源（交易所 + 券商加收 + 滑点 tick），
    差异只在：V5X 的成本模型把滑点算成 ``slip_yuan``（元/手）而非改写成交价。
    """

    def _side(act: str) -> tuple[float, float, float]:
        tc = fee_per_lot(symbol, act, on_date=on_date)
        fx = pct = 0.0
        if tc.fee_type == "FIXED":
            fx += tc.exchange_fee_yuan
        elif tc.fee_type == "PCT":
            pct += tc.exchange_fee_yuan / 10000.0
        if tc.broker_markup_type == "FIXED":
            fx += tc.broker_markup_yuan
        elif tc.broker_markup_type == "PCT":
            pct += tc.broker_markup_yuan / 10000.0
        return fx, pct, float(tc.slip_ticks or 0)

    fo, po, st = _side("OPEN")
    ft, pt, _ = _side("CLOSE_TODAY")
    fy, py, _ = _side("CLOSE_YEST")
    sp = variety_spec(symbol)
    tick = float(sp["tick_size"])
    return CostCfg(
        fee_row={"OPEN": (fo, po), "CLOSE_TODAY": (ft, pt), "CLOSE_YEST": (fy, py)},
        slip_yuan=st * tick,
        mult=float(sp["multiplier"]),
        tick=tick,
    )


# ---------------------------------------------------------------- 装载
def load_market_data(session, symbols: list[str], start: date, end: date, *,
                     src: str = "bar_60m_ba",
                     regime_mode: str = "frozen") -> V5XMarketData:
    """一次装载 ``symbols`` 在 [start, end) 的全部上下文。"""
    open_days = load_open_days(session, start, end)
    bars = _load_bars(session, symbols, start, end, src)
    md = V5XMarketData(src=src, open_days=open_days)
    md.diagnostics = {
        "n_bars": int(len(bars)),
        "n_symbols": int(bars["symbol"].nunique()) if len(bars) else 0,
        "calendar_days": int(len(open_days)),
        "regime_mode": regime_mode,
        "member_z_topn": MEMBER_Z_TOPN,
        "calendar_fallback": bool(not len(open_days)),
    }
    if bars.empty:
        return md

    md.bars = {str(s): g.reset_index(drop=True)
               for s, g in bars.groupby("symbol", sort=False)}
    daily = _daily_frame(bars, open_days)
    md.hvol = _hvol_flags(daily)
    md.blocked = _cluster_shock(daily)
    md.regime = _regime_table(daily, regime_mode)
    md.sess_last_hours = _sess_last_hours(bars, open_days)
    md.member_z = _member_ts_z(session, symbols, start, end)
    md.diagnostics["n_member_z_symbols"] = len(md.member_z)
    md.diagnostics["n_blocked_clusters"] = len(md.blocked)
    return md


def build_ctx(md: V5XMarketData, symbol: str, td) -> DailyCtx:
    """组装某个交易日的 ``DailyCtx``（只使用 <= T-1 的信息，见模块 docstring）。"""
    z = np.nan
    avz = md.member_z.get(symbol)
    if avz is not None:
        days, zs = avz
        j = int(np.searchsorted(days, np.datetime64(td, "D"), side="right")) - 1
        if j >= 0:
            z = float(zs[j]) if np.isfinite(zs[j]) else np.nan
    cl = CLUSTER.get(symbol[:-3] if symbol.endswith("888") else symbol)
    rg, vz = md.regime.get((symbol, np.datetime64(td, "D")), (None, None))
    return DailyCtx(
        td=td,
        hvol=bool(md.hvol.get(symbol, {}).get(np.datetime64(td, "D"), False)),
        member_z=z,
        blocked=bool(cl and td in md.blocked.get(str(cl), set())),
        sess_last_hours=md.sess_last_hours.get(symbol, frozenset()),
        regime=rg,
        vol_z=vz,
        pos_scale=1.0,
    )
