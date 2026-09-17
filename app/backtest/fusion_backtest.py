"""
融合策略回测与研究模块（报告 G1 / PRD §8 融合策略两层回测）

设计要点（满足报告 §14 #1「单一真源」铁律）：
- **信号层**：直接复用 `app.strategies.fusion_signal.walk_fusion_states`——
  与线上实时引擎 `fusion_state_detail` 是**同一套循环**（逐帧 yield），
  因此回测信号与实盘信号逐位一致，不存在口径分叉。
- **walk-forward 天然成立**：`walk_fusion_states` 在每根 bar i 上只用 `≤ i` 的数据，
  无前视；回测逐帧消费即可，无需额外切窗。
- **组合层**：单品种 → 逐笔成交（close-only，按信号翻转 / 止损 / 保本离场） →
  FIFO 顺序汇总成组合净值曲线与指标。

两层：
  ① 信号层 = 生成 per-bar 目标持仓（state 0/1/2）
  ② 组合层 = 把 state 序列重放成成交序列，扣除成本，统计收益/回撤/夏普

参数面板（§8.4）：sl_atr / trail_atr / be_r / W / ma_n / atr_n / ema_k /
entry_mode / cooldown_bars / max_positions / cost_bp / src / seed（多 seed 鲁棒性）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import logger
from app.models import HourlyBar
from app.strategies.fusion_signal import walk_fusion_states

TZ = "Asia/Shanghai"


@dataclass
class FusionBacktestParams:
    """融合策略回测参数（= 实盘参数 + 回测专用参数）。"""
    # —— 与 fusion_state_detail 同口径（单一真源需要逐位对齐）——
    ema_k: int = 140
    atr_n: int = 14
    ma_n: int = 20
    sl_atr: float = 2.0
    trail_atr: float = 2.0
    be_r: float = 0.5
    W: int = 60
    entry_mode: str = "both_nm"
    cooldown_bars: int = 3
    min_bars: int = 160
    max_bars: int = 400
    # —— 回测专用 ——
    src: str = "akshare"             # 单一源（杜绝双源混读，§14 #2）
    cost_bp: float = 1.3             # 双边成本（基点）；pnl 中扣 2×单边 = 整段 cost_bp
    max_positions: int = 1           # 单品种同时持仓数（1=单仓）
    seed: int = 0                    # 多 seed 鲁棒性：扰动止损用
    seed_jitter: float = 0.0         # sl/trail 相对扰动幅度（0=关）；多 seed 时各 seed 取不同值
    lookback_bars: int = 0           # 0=全历史 walk-forward；>0 则仅用最后 N 根


def fusion_params_from_config(overrides: dict | None = None) -> FusionBacktestParams:
    """以 config.fusion 为基准，叠加回测覆盖参数。"""
    cfg = get_settings().fusion
    base = dict(
        ema_k=cfg.ema_k, atr_n=cfg.atr_n, ma_n=cfg.ma_n,
        sl_atr=cfg.sl_atr, trail_atr=cfg.trail_atr, be_r=cfg.be_r,
        W=cfg.W, entry_mode=cfg.entry_mode, cooldown_bars=cfg.cooldown_bars,
        min_bars=cfg.min_bars, max_bars=cfg.max_bars,
        src=cfg.hourly_src,
    )
    p = FusionBacktestParams(**base)
    if overrides:
        for k, v in overrides.items():
            if hasattr(p, k):
                setattr(p, k, v)
    return p


def _read_hourly(session, symbol: str, src: str, start: date | None, end: date | None):
    """单源读取小时线，按时间升序返回 ohlc DataFrame（含 dt）。"""
    q = (
        select(
            HourlyBar.trade_datetime, HourlyBar.open, HourlyBar.high,
            HourlyBar.low, HourlyBar.close,
        )
        .where(HourlyBar.symbol == symbol)
        .where(HourlyBar.src == src)
        .order_by(HourlyBar.trade_datetime.asc())
    )
    if start:
        q = q.where(HourlyBar.trade_datetime >= pd.Timestamp(start))
    if end:
        q = q.where(HourlyBar.trade_datetime <= pd.Timestamp(end))
    rows = session.execute(q).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def _htf_direction(c: np.ndarray, ema_k: int) -> np.ndarray:
    """EMA140 方向门控（与 evaluate_symbol 一致：用前一根方向对齐，无前视）。"""
    if len(c) < ema_k:
        return np.zeros(len(c), dtype=int)
    ema140 = pd.Series(c).ewm(span=ema_k, adjust=False).mean().to_numpy()
    dir_raw = np.where(c > ema140, 1, -1)
    return np.concatenate([[0], dir_raw[:-1]])


def _generate_states(df: pd.DataFrame, p: FusionBacktestParams) -> pd.DataFrame:
    """信号层：消费 walk_fusion_states，输出 per-bar (dt, close, state)。

    注意对齐：生成器从 i=2 开始（前两根为预热），故 state[k] 对应 df 行 k+2。
    """
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    htf = _htf_direction(c, p.ema_k)
    n = len(c)
    states = np.zeros(n, dtype=int)
    for k, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
        states[k + 2] = d["state"]
    out = df.reset_index(drop=True).copy()
    out["state"] = states
    return out


def _simulate_trades(states_df: pd.DataFrame, p: FusionBacktestParams) -> list[dict]:
    """组合层：把 state 序列重放成逐笔成交（close-only，单仓）。

    规则：bar i 收盘后策略目标持仓 = state_i，故在 bar i 的收盘价开/平。
    - 目标由 0→1/2 或翻转：在 c[i] 开仓（成交价 c[i]）
    - 目标回到 0 或反向：在 c[i] 平仓（成交价 c[i]）
    止损/保本离场已蕴含在 state 翻转中（引擎 state 归零即触发离场）。
    """
    dts = states_df["dt"].to_numpy()
    closes = states_df["close"].to_numpy(float)
    states = states_df["state"].to_numpy()
    cost_frac = p.cost_bp / 10000.0  # 整段双边成本（小数）
    trades: list[dict] = []
    pos = 0        # 当前持仓方向 0/1/2
    entry_px = 0.0
    entry_i = -1
    for i in range(len(states)):
        tgt = int(states[i])
        if tgt == pos:
            continue
        px = closes[i]
        # 平仓（若有）
        if pos != 0:
            dirn = 1.0 if pos == 1 else -1.0
            gross = dirn * (px - entry_px)
            pnl = gross - cost_frac * entry_px  # 扣双边成本
            trades.append({
                "symbol": None,
                "side": "LONG" if pos == 1 else "SHORT",
                "entry_dt": dts[entry_i], "exit_dt": dts[i],
                "entry_px": round(float(entry_px), 4), "exit_px": round(float(px), 4),
                "pnl": round(float(pnl), 4),
                "pnl_pct": round(float(pnl / entry_px * 100.0), 4) if entry_px else 0.0,
            })
        # 开仓（若目标非 0）
        if tgt != 0:
            pos = tgt
            entry_px = px
            entry_i = i
        else:
            pos = 0
    # 末尾仍持仓：以最后收盘价强平（标记未实现，不计入已平仓统计）
    if pos != 0:
        px = closes[-1]
        dirn = 1.0 if pos == 1 else -1.0
        gross = dirn * (px - entry_px)
        pnl = gross - cost_frac * entry_px
        trades.append({
            "symbol": None, "side": "LONG" if pos == 1 else "SHORT",
            "entry_dt": dts[entry_i], "exit_dt": dts[-1],
            "entry_px": round(float(entry_px), 4), "exit_px": round(float(px), 4),
            "pnl": round(float(pnl), 4),
            "pnl_pct": round(float(pnl / entry_px * 100.0), 4) if entry_px else 0.0,
            "open": True,
        })
    return trades


def _metrics_from_trades(trades: list[dict]) -> dict:
    closed = [t for t in trades if not t.get("open")]
    n = len(closed)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "total_return_pct": 0.0,
                "avg_pnl_pct": None, "max_drawdown_pct": 0.0, "sharpe": None}
    wins = [t for t in closed if t["pnl"] > 0]
    pcts = np.array([t["pnl_pct"] for t in closed], dtype=float)
    eq = np.cumprod(1.0 + pcts / 100.0)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak * 100.0
    mean = float(pcts.mean())
    std = float(pcts.std(ddof=1)) if n > 1 else 0.0
    sharpe = round(mean / std * np.sqrt(50), 3) if std > 0 else None  # ≈ 50 笔/年近似年化
    return {
        "n_trades": n,
        "n_wins": len(wins),
        "win_rate": round(len(wins) / n, 4),
        "avg_pnl_pct": round(mean, 4),
        "total_return_pct": round(float((eq[-1] - 1.0) * 100.0), 4),
        "max_drawdown_pct": round(float(dd.max()), 4),
        "sharpe": sharpe,
        "equity_curve": [round(float(x), 4) for x in eq.tolist()],
    }


def _jittered_params(p: FusionBacktestParams, seed: int, jitter: float) -> FusionBacktestParams:
    """多 seed：对 sl/trail 施加确定性扰动，评估参数鲁棒性。"""
    if jitter <= 0:
        return p
    # 用 seed 生成 [-jitter, +jitter] 的确定偏移
    rng = np.random.default_rng(seed)
    f = 1.0 + rng.uniform(-jitter, jitter)
    q = FusionBacktestParams(**{k: getattr(p, k) for k in p.__dataclass_fields__})
    q.sl_atr = round(p.sl_atr * f, 4)
    q.trail_atr = round(p.trail_atr * f, 4)
    q.seed = seed
    return q


def run_fusion_backtest(session, symbols: list[str] | None = None,
                        params: FusionBacktestParams | None = None,
                        start: date | None = None, end: date | None = None,
                        seeds: list[int] | None = None) -> dict:
    """执行融合策略回测（信号层 walk-forward + 组合层 FIFO 重放）。

    seeds：非空则对每个 seed 跑一遍（用 seed_jitter 扰动），最终指标取均值。
    """
    settings = get_settings()
    if params is None:
        params = fusion_params_from_config()
    if symbols is None:
        symbols = [s.symbol for s in settings.main_contracts]
    all_trades: list[dict] = []
    per_symbol: list[dict] = []
    skipped: list[str] = []

    seed_list = seeds if seeds else [params.seed]
    for sym in symbols:
        sym_trades: list[dict] = []
        sym_metrics_acc: list[dict] = []
        for sd in seed_list:
            pp = _jittered_params(params, sd, params.seed_jitter) if len(seed_list) > 1 else params
            df = _read_hourly(session, sym, pp.src, start, end)
            if df is None or len(df) < pp.min_bars:
                continue
            if pp.lookback_bars and len(df) > pp.lookback_bars:
                df = df.iloc[-pp.lookback_bars:]
            states_df = _generate_states(df, pp)
            tr = _simulate_trades(states_df, pp)
            for t in tr:
                t["symbol"] = sym
            sym_trades.extend(tr)
            sym_metrics_acc.append(_metrics_from_trades(tr))
        if not sym_trades:
            skipped.append(sym)
            continue
        m = _metrics_from_trades(sym_trades)
        m["symbol"] = sym
        per_symbol.append(m)
        all_trades.extend(sym_trades)

    agg = _metrics_from_trades(all_trades)
    agg["per_symbol"] = per_symbol
    agg["skipped"] = skipped
    agg["params"] = {k: getattr(params, k) for k in params.__dataclass_fields__}
    agg["seeds"] = seed_list
    return agg


def run_fusion_matrix(session, symbols: list[str] | None = None,
                     start: date | None = None, end: date | None = None,
                     grid: dict | None = None) -> dict:
    """四维测试矩阵（§8.3 简化版）：在 (src × sl_atr × trail_atr) 网格上跑回测，汇总净收益/胜率。

    grid 形如 {"src": [...], "sl_atr": [...], "trail_atr": [...]}
    """
    settings = get_settings()
    if symbols is None:
        symbols = [s.symbol for s in settings.main_contracts[:10]]  # 矩阵默认抽样 10 品种控时
    grid = grid or {
        "src": ["akshare", "tqsdk"],
        "sl_atr": [1.5, 2.0, 2.5],
        "trail_atr": [1.5, 2.0, 2.5],
    }
    base = fusion_params_from_config()
    rows = []
    for src in grid["src"]:
        for sl in grid["sl_atr"]:
            for tr in grid["trail_atr"]:
                p = FusionBacktestParams(**{
                    k: getattr(base, k) for k in base.__dataclass_fields__
                })
                p.src = src
                p.sl_atr = sl
                p.trail_atr = tr
                res = run_fusion_backtest(session, symbols, p, start, end)
                rows.append({
                    "src": src, "sl_atr": sl, "trail_atr": tr,
                    "n_trades": res.get("n_trades", 0),
                    "win_rate": res.get("win_rate"),
                    "total_return_pct": res.get("total_return_pct"),
                    "max_drawdown_pct": res.get("max_drawdown_pct"),
                    "sharpe": res.get("sharpe"),
                })
    return {"grid": grid, "symbols": symbols, "rows": rows}


if __name__ == "__main__":
    from app.core.db import session_scope

    with session_scope() as s:
        r = run_fusion_backtest(s, symbols=["FG888", "RB888", "CU888"],
                                start=date(2025, 1, 1), end=date(2026, 9, 1))
        print("n_trades=", r["n_trades"], "win_rate=", r["win_rate"],
              "total_return_pct=", r["total_return_pct"], "max_dd=", r["max_drawdown_pct"])
