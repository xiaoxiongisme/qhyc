#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用信号 WF-OOS 回测底座（2026-11-01，研究分支 cb/v2-oos-investigation，未碰 main/cb/dev）。

目的：为「新信号源」候选（B1 截面动量 / B3 配对回归 / …）提供**与 V2 §8 同口径**的
严格 OOS 评估底座，使每个候选都能直接套用 §6 的准入门槛（净R>0、PF≥1.1、
2024/2025/2026 每年不崩、过安慰剂、与V2低相关）。

口径（单一真源，与 docs/V2样本外崩塌排查.md §8 对齐）：
  - 行情：l1_mkt.bar_60m（按 symbol 取 60m，重采样为日线 open/close/high/low），
    与 fusion 回测同一张表（仅主连 888 可用，故 B1 基差 carry 不适用，改用截面动量）。
  - 成本：app.data.cost 的 cost_coefficients + cost_points_at，按**建仓日**历史费率、
    真实主力合约码（main_contract_at）取真实开平成本点数 —— 与 fusion 逐笔口径一致。
  - OOS：walk-forward 测试窗（train720/test60/step60/warmup150, reestimate=False），
    仅收集 entry_date >= te_start 的成交（与 §8 同 OOS 定义，使 2024/2025/2026 子期对齐）。
  - R 口径：逐笔 R = (pnl_points - cost_points) / risk_points，risk = sl_mult × 日线 ATR。
    注：本底座用日线 ATR（sl_mult 默认 3）作风险分母，与 fusion 的 60m sl_atr=1.0 分母
    不同；R 的绝对量级不可直接跨研究比较，但**符号/PF/分年稳定性**的判定结论可比。
  - 前视控制：信号在每日收盘后计算（target[t] 仅用 ≤ t-1 的数据，靠 shift 实现），
    成交在**次日 open** 执行（open-to-open），杜绝前视。

模拟假设：每品种单手、不金字塔；固定 rebalance_n 交易日调仓；窗口结束强制平仓
（不跨窗持仓，保证 OOS 自洽）。无 intra-trade 止损（R 仅作风险归一分母）。

用法（被 signal_b1_*.py / signal_b3_*.py 调用）：
  from signal_oos_backtest import run_signal_wf, load_market, compute_targets_dummy
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from app.data.cost import (
    cost_coefficients, cost_points_at, main_contract_at, _variety,
)

BARS_PER_DAY = 6.7
SL_MULT = 3.0          # 日线 ATR 倍数，作为每笔风险分母
REBALANCE_N = 5         # 默认每 5 个交易日调仓（周频）


# ------------------------------------------------------------------- 数据
def load_market(session, symbols: list[str], start: date, end: date):
    """一次性取全部品种 60m，重采样为日线 open/close/high/low（date×symbol）。

    返回 (opens, closes, highs, lows) 四个 DataFrame，index=date，columns=symbol。
    缺失 (symbol,date) 为 NaN（不同品种交易历不同，由信号/模拟跳过）。
    """
    rows = session.execute(text(
        "SELECT symbol, bucket, open, high, low, close FROM l1_mkt.bar_60m "
        "WHERE bucket >= :s AND bucket <= :e"
    ), {"s": pd.Timestamp(start), "e": pd.Timestamp(end)}).all()
    raw = pd.DataFrame(rows, columns=["symbol", "dt", "open", "high", "low", "close"])
    for c in ("open", "high", "low", "close"):
        raw[c] = raw[c].astype(float)
    raw["date"] = raw["dt"].dt.floor("D")

    all_dates = set()
    per_sym: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        sub = raw[raw["symbol"] == sym]
        if sub.empty:
            continue
        daily = sub.groupby("date").agg(
            open=("open", "first"), close=("close", "last"),
            high=("high", "max"), low=("low", "min"),
        ).sort_index()
        per_sym[sym] = daily
        all_dates.update(daily.index)

    dates = sorted(all_dates)
    idx = pd.DatetimeIndex(dates)
    opens = pd.DataFrame(index=idx, columns=symbols, dtype=float)
    closes = pd.DataFrame(index=idx, columns=symbols, dtype=float)
    highs = pd.DataFrame(index=idx, columns=symbols, dtype=float)
    lows = pd.DataFrame(index=idx, columns=symbols, dtype=float)
    for sym, daily in per_sym.items():
        opens[sym] = daily["open"]
        closes[sym] = daily["close"]
        highs[sym] = daily["high"]
        lows[sym] = daily["low"]
    # 统一 index 为 date 类型（去 tz），便于按 te_s/te_e 切片
    for f in (opens, closes, highs, lows):
        f.index = f.index.date
    return opens, closes, highs, lows


def daily_atr(highs: pd.DataFrame, lows: pd.DataFrame, closes: pd.DataFrame,
              n: int = 20) -> pd.DataFrame:
    """日线真实波幅的 n 日滚动均值（date×symbol），用于风险分母。"""
    tr = pd.DataFrame(index=closes.index, columns=closes.columns, dtype=float)
    for sym in closes.columns:
        h, l, c = highs[sym], lows[sym], closes[sym]
        prev_c = c.shift(1)
        rng = pd.concat([(h - l),
                         (h - prev_c).abs(),
                         (l - prev_c).abs()], axis=1).max(axis=1)
        tr[sym] = rng.rolling(n, min_periods=max(5, n // 2)).mean()
    return tr


# ------------------------------------------------------------------- 指标
def seg_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    g = np.array([r["gross"] for r in rows], dtype=float)
    c = np.array([r["cost"] for r in rows], dtype=float)
    n = np.array([r["net"] for r in rows], dtype=float)
    wins = n > 0
    losses = n < 0
    sum_win = float(n[wins].sum()) if wins.any() else 0.0
    sum_loss = float(n[losses].sum()) if losses.any() else 0.0
    pf = (sum_win / abs(sum_loss)) if sum_loss != 0 else (float("inf") if sum_win > 0 else 0.0)
    return {
        "n": int(len(rows)),
        "win_rate": round(float(wins.mean()) * 100, 2),
        "mean_gross_R": round(float(g.mean()), 5),
        "mean_cost_R": round(float(c.mean()), 5),
        "mean_net_R": round(float(n.mean()), 5),
        "sum_net_R": round(float(n.sum()), 1),
        "profit_factor": (round(pf, 3) if pf != float("inf") else "inf"),
    }


# ------------------------------------------------------------------- 模拟
def simulate(targets: pd.DataFrame, opens: pd.DataFrame, closes: pd.DataFrame,
             atr: pd.DataFrame, rebalance_n: int = REBALANCE_N,
             sl_mult: float = SL_MULT) -> list[dict]:
    """open-to-open 模拟。targets 已用 ≤t-1 数据计算（外部 shift），本函数只在 rebalance
    日按 target 调仓，窗口结束强制平仓（不跨窗）。返回逐笔成交（含 R 口径）。

    targets: date×symbol ∈ {-1,0,1}；opens/closes/atr: date×symbol。
    """
    dates = list(targets.index)
    syms = list(targets.columns)
    trades: list[dict] = []
    # 全局 rebalance 标尺：从数据起点起每 rebalance_n 个交易日一次
    # 用日期位置做模，保证各窗一致
    pos = {s: 0 for s in syms}
    entry = {}
    for i, d in enumerate(dates):
        is_rebal = (i % rebalance_n == 0)
        is_last = (i == len(dates) - 1)
        for s in syms:
            tgt = targets.at[d, s]
            if pd.isna(tgt):
                tgt = 0
            else:
                tgt = int(round(tgt))
            cur = pos[s]
            op = opens.at[d, s] if d in opens.index else np.nan
            # 强制平仓（窗口末或信号变 0/反向）
            if cur != 0 and (is_last or is_rebal) and tgt != cur:
                if not pd.isna(op) and s in entry:
                    e = entry[s]
                    dirn = 1.0 if cur == 1 else -1.0
                    pnl = dirn * (op - e["px"])
                    risk = sl_mult * e["atr"]
                    if risk and np.isfinite(risk) and risk > 0:
                        cost_pts = e["cost_pts"]
                        net = pnl - cost_pts
                        R = net / risk
                        trades.append({
                            "symbol": s, "side": "LONG" if cur == 1 else "SHORT",
                            "entry_dt": e["dt"], "exit_dt": d,
                            "entry_px": e["px"], "exit_px": op,
                            "pnl": pnl, "cost_pts": cost_pts, "risk": risk,
                            "gross": pnl / risk, "cost": cost_pts / risk, "net": R,
                            "holding_days": (d - e["dt"]).days,
                        })
                    pos[s] = 0
                    entry.pop(s, None)
            # 开仓（rebalance 日且 target 非零且与当前不同；避免末日开仓）
            if tgt != 0 and tgt != cur and is_rebal and not is_last:
                o = opens.at[d, s] if d in opens.index else np.nan
                a = atr.at[d, s] if d in atr.index else np.nan
                if pd.isna(o) or pd.isna(a) or not np.isfinite(a) or a <= 0:
                    continue
                # 真实成本（建仓日历史费率 + 真实主力合约码）
                try:
                    ctr = main_contract_at(_variety(s), d)
                    coef = cost_coefficients(s, contract=ctr,
                                            close_action="CLOSE_YEST", on_date=d)
                    cost_pts = cost_points_at(coef, o)
                except Exception:
                    cost_pts = 0.0
                pos[s] = tgt
                entry[s] = {"px": o, "atr": a, "dt": d, "cost_pts": cost_pts}
    return trades


# ------------------------------------------------------------------- 安慰剂
def permute_targets(targets: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """标签置换安慰剂：每个 rebalance 日，保持「多头数/空头数」不变，但随机重选品种。
    破坏信号的选择性，同时保留暴露数量结构 —— 与 §8.8 的「随机同结构子集」逻辑一致。"""
    out = targets.copy()
    for d in targets.index:
        row = targets.loc[d].values.astype(float)
        longs = [j for j, v in enumerate(row) if v > 0]
        shorts = [j for j, v in enumerate(row) if v < 0]
        nL, nS = len(longs), len(shorts)
        if nL == 0 and nS == 0:
            continue
        ncol = len(row)
        # 随机不放回选 nL 个多头、nS 个空头（从全部列中）
        perm = rng.permutation(ncol)
        new = np.zeros(ncol, dtype=float)
        if nL:
            new[perm[:nL]] = 1.0
        if nS:
            new[perm[nL:nL + nS]] = -1.0
        out.loc[d] = new
    return out


def placebo_agg(targets, opens, closes, atr, wfp, start, end, rebalance_n,
                sl_mult, actual_agg, n_perm=300, seed=20261101):
    """标签置换安慰剂：与 actual 用**完全相同的窗口模拟逻辑**（simulate_windows），
    仅把每 rebalance 日的多/空品种随机重选（保持暴露数量），破坏信号选择性。
    pl_ge% = 置换后 agg_net >= 实际 agg_net 的比例（越低越真）。"""
    rng = np.random.default_rng(seed)
    ge = 0
    counted = 0
    for _ in range(n_perm):
        pt = permute_targets(targets, rng)
        tr = simulate_windows(pt, opens, closes, atr, wfp, start, end,
                              rebalance_n, sl_mult)
        s = seg_stats(tr)
        if s.get("n", 0) == 0:
            continue
        counted += 1
        if s["sum_net_R"] >= actual_agg:
            ge += 1
    return round(ge / counted * 100, 1) if counted else float("nan")


def simulate_windows(targets, opens, closes, atr, wfp, start, end,
                     rebalance_n, sl_mult) -> list[dict]:
    """与 actual 一致的窗口模拟：逐 WF 窗切片（含 warmup 历史），只收 entry>=te_s 成交。"""
    all_trades: list[dict] = []
    for tr_s, tr_e, te_s, te_e in _windows(start, end, wfp):
        mask = ((targets.index >= (te_s - timedelta(days=wfp.warmup_days))) &
                (targets.index <= te_e))
        if not mask.any():
            continue
        tr = simulate(targets[mask], opens[mask], closes[mask], atr[mask],
                     rebalance_n, sl_mult)
        for t in tr:
            if _dt(t["entry_dt"]) is not None and _dt(t["entry_dt"]).date() >= te_s:
                all_trades.append(t)
    return all_trades


# ------------------------------------------------------------------- 编排
def run_signal_wf(signal_fn, signal_name: str, symbols: list[str],
                  start: date, end: date, rebalance_n: int = REBALANCE_N,
                  sl_mult: float = SL_MULT, out: str | None = None,
                  do_placebo: bool = True, **sig_kwargs) -> dict:
    """端到端：加载行情 → 计算 targets（全历史一次）→ 逐 WF 窗模拟 → 聚合 + 安慰剂。"""
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    # 数据起点前推 400 天，保证首个窗口 warmup/信号 trailing 窗口有历史
    data_start = start - timedelta(days=400)
    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    atr = daily_atr(highs, lows, closes)
    session.close(); engine.dispose()

    # 信号（全历史，内部自管前视 shift）
    targets = signal_fn(closes, opens=opens, **sig_kwargs)
    targets = targets.reindex(index=opens.index, columns=symbols).fillna(0.0)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    all_trades = simulate_windows(targets, opens, closes, atr, wfp, start, end,
                                  rebalance_n, sl_mult)
    n_windows = len(list(_windows(start, end, wfp)))

    rows = all_trades
    overall = seg_stats(rows)
    by_year: dict = defaultdict(list)
    for r in rows:
        by_year[_dt(r["entry_dt"]).year].append(r)
    by_year_stats = {str(y): seg_stats(v) for y, v in sorted(by_year.items())}

    # 暴露（平均同时在场手数 / 占总可能）
    exposure = round(len(rows) / max(1, n_windows) / max(1, len(symbols)), 3)

    report = {
        "signal": signal_name,
        "meta": {"symbols": len(symbols), "n_windows": n_windows,
                 "start": str(start), "end": str(end),
                 "rebalance_n": rebalance_n, "sl_mult": sl_mult,
                 "bars_per_day": BARS_PER_DAY},
        "overall": overall,
        "by_year": by_year_stats,
        "exposure_avg_open_per_window_per_symbol": exposure,
        "n_trades": len(rows),
    }
    # 安慰剂（与 actual 同窗口逻辑，300 次置换）
    if rows and do_placebo:
        actual_agg = overall["sum_net_R"]
        report["placebo"] = {
            "rand_same_structure_ge_actual_pct": placebo_agg(
                targets, opens, closes, atr, wfp, start, end, rebalance_n,
                sl_mult, actual_agg),
        }
    if out:
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                             encoding="utf-8")
    return report


# ------------------------------------------------------------------- 控制台
def print_report(report: dict):
    o = report["overall"]
    print(f"\n===== 信号 {report['signal']} WF-OOS =====")
    print(f"meta: {report['meta']}")
    print(f"overall: n={o.get('n')} 胜率={o.get('win_rate')}% 净R={o.get('mean_net_R')} "
          f"PF={o.get('profit_factor')} sumNetR={o.get('sum_net_R')}")
    for y, st in report["by_year"].items():
        print(f"  {y}: n={st.get('n')} 净R={st.get('mean_net_R')} PF={st.get('profit_factor')} "
              f"胜率={st.get('win_rate')}%")
    if "placebo" in report:
        print(f"  安慰剂 rand_ge_actual% = {report['placebo']['rand_same_structure_ge_actual_pct']}")


if __name__ == "__main__":
    # 独立运行示例：需由调用方传入 signal_fn；此处仅打印用法
    print("此模块为底座库，由 signal_b1_momentum.py / signal_b3_pairs.py 调用。")
