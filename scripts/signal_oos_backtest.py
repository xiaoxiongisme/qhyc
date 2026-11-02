#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用信号 WF-OOS 回测底座（2026-11-02 修订：消除安慰剂置换内循环里的逐笔 DB 成本查询）。

研究分支 cb/v2-oos-investigation，未碰 main/cb/dev。

目的：为「新信号源」候选（B1 截面动量 / B3 配对回归 / …）提供**与 V2 §8 同口径**的
严格 OOS 评估底座，使每个候选都能直接套用 §6 的准入门槛（净R>0、PF≥1.1、
2024/2025/2026 每年不崩、过安慰剂、与V2低相关）。

口径（单一真源，与 docs/V2样本外崩塌排查.md §8 对齐）：
  - 行情：l1_mkt.bar_60m（按 symbol 取 60m，重采样为日线 open/close/high/low），
    与 fusion 回测同一张表（仅主连 888 可用，故 B1 基差 carry 不适用，改用截面动量）。
  - 成本：app.data.cost 的 cost_coefficients + cost_points_at，按**建仓日**历史费率、
    真实主力合约码（main_contract_at）取真实开平成本点数 —— 与 fusion 逐笔口径一致。
    ★ 修订点：main_contract_at / cost_coefficients 原来在 simulate 内循环里「逐笔、
    逐建仓日」查库，置换后（尤其 B3 日频翻仓 rebalance_n=1）开仓数从 ~100 爆到 ~4700/次，
    单次置换 225s（其中 ~211s 是 DB）。现改为：run_signal_wf 一次性用「每品种一条 roll
    历史 SQL + bisect」预建 contract_map[(symbol,date)]，并把 cost_coefficients 按
    (symbol, contract) 缓存进 coef_cache，simulate 内零 DB 调用 —— 成本数值与原口径逐位一致。
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
  from signal_oos_backtest import run_signal_wf
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
from bisect import bisect_right

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
def load_market(session, symbols: list[str], start: date, end: date,
                calendar_mode: str = "raw"):
    """一次性取全部品种 60m，重采样为日线 open/close/high/low（date×symbol）。

    返回 (opens, closes, highs, lows) 四个 DataFrame，index=date，columns=symbol。
    缺失 (symbol,date) 为 NaN（不同品种交易历不同，由信号/模拟跳过）。

    calendar_mode（六期新增，默认 'raw' 保持既有研究逐位一致）：
      'raw'          —— 原口径：raw["date"] = bucket.floor("D")，日历取品种并集。
      'weekend_drop' —— 剔除本地周六/周日 bar（保守简化）。
      'session_map'  —— 夜盘归属下一交易日：hour>=21 归次日；周六/周日往后推到周一。
    原因（六期 §26 实证）：SC/AG/AU 等夜盘跨到周六凌晨的 bar 经 floor("D")
    被记成「周六」，日历取品种并集后这些伪交易日进入 index；FU 夜盘不跨日、
    周六无 bar → open=NaN，曾被误读为「FU888 缺 15.2%」。剔除/归位后 FU888
    真实交易日覆盖率 99.95%。l3_ref.trade_calendar 仅记开市日且覆盖不全，
    故 session_map 只按星期推、不处理法定节假日（诚实标注为简化）。
    """
    rows = session.execute(text(
        "SELECT symbol, bucket, open, high, low, close FROM l1_mkt.bar_60m "
        "WHERE bucket >= :s AND bucket <= :e"
    ), {"s": pd.Timestamp(start), "e": pd.Timestamp(end)}).all()
    raw = pd.DataFrame(rows, columns=["symbol", "dt", "open", "high", "low", "close"])
    for c in ("open", "high", "low", "close"):
        raw[c] = raw[c].astype(float)
    if calendar_mode == "raw":
        raw["date"] = raw["dt"].dt.floor("D")
    elif calendar_mode == "weekend_drop":
        raw["date"] = raw["dt"].dt.floor("D")
        # dt.dayofweek: 0=Mon..6=Sun，与 floor("D") 同为本地时区口径
        raw = raw[raw["dt"].dt.dayofweek < 5]
    elif calendar_mode == "session_map":
        def _session_date(ts):
            d = ts.date()
            if ts.hour >= 21:
                d += timedelta(days=1)
            while d.weekday() >= 5:            # 5=Sat, 6=Sun → 推到下周一
                d += timedelta(days=1)
            return pd.Timestamp(d)
        raw["date"] = raw["dt"].map(_session_date)
    else:
        raise ValueError(f"unknown calendar_mode={calendar_mode!r}")

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


# ------------------------------------------------------------------- 成本面预计算
def main_contract_surface(session, symbols: list[str], dates) -> dict:
    """一次性预建 contract_map[(symbol, date)] -> 主力合约码 | None。

    原来 simulate 内循环对每个开仓日调 main_contract_at(variety, date) —— 逐 (品种,日期)
    一条 SQL，置换后开仓数爆增导致每置换 200s+。本函数改为：**每个品种只发一条** roll 历史
    SQL（main_contract_map 全表按品种），再对每个目标日 bisect 取「<= d 的最近一条」。
    结果与 main_contract_at 逐位一致（同一张表、同一 latest<=d 语义），但 DB 调用从
    O(开仓数×置换数) 降到 O(品种数)。
    """
    roll_hist: dict[str, list] = {}   # variety -> [(trade_date, contract), ...] 升序
    for sym in symbols:
        v = _variety(sym)
        if v in roll_hist:
            continue
        rows = session.execute(text(
            "SELECT trade_date, COALESCE(underlying, main_symbol) FROM main_contract_map "
            "WHERE upper(product) = :v ORDER BY trade_date"
        ), {"v": v}).all()
        hist = []
        for r in rows:
            td = r[0].date() if isinstance(r[0], datetime) else r[0]
            hist.append((td, r[1]))
        hist.sort(key=lambda x: x[0])
        roll_hist[v] = hist
    surface: dict = {}
    for sym in symbols:
        v = _variety(sym)
        hist = roll_hist[v]
        tds = [h[0] for h in hist]
        for d in dates:
            dd = d.date() if isinstance(d, datetime) else d
            j = bisect_right(tds, dd) - 1
            surface[(sym, d)] = hist[j][1] if j >= 0 else None
    return surface


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
             sl_mult: float = SL_MULT, contract_map: dict | None = None,
             coef_cache: dict | None = None) -> list[dict]:
    """open-to-open 模拟（numpy 位置索引版）。targets 已用 <=t-1 数据
    计算（外部 shift），本函数只在 rebalance 日按 target 调仓，窗口结束强制平仓（不跨窗）。
    targets: date×symbol ∈ {-1,0,1}；opens/closes/atr: date×symbol。

    contract_map / coef_cache：由 run_signal_wf 预建的成本面，使内循环零 DB 调用；
    传 None 时回退到原「逐开仓日查库」路径（用于离线单步调试）。
    """
    syms = list(targets.columns)
    ncol = len(syms)
    idx = targets.index
    nrow = len(idx)
    dates = np.array([d for d in idx], dtype=object)
    T = np.asarray(targets.to_numpy(), dtype=float)
    O = np.asarray(opens.reindex(idx).to_numpy(), dtype=float)
    A = np.asarray(atr.reindex(idx).to_numpy(), dtype=float)
    trades: list[dict] = []
    pos = [0] * ncol
    e_px = [0.0] * ncol
    e_atr = [0.0] * ncol
    e_cost = [0.0] * ncol
    e_i = [-1] * ncol
    for i in range(nrow):
        is_rebal = (i % rebalance_n == 0)
        is_last = (i == nrow - 1)
        di = dates[i]
        for s in range(ncol):
            tgt_v = T[i, s]
            tgt = 0 if (tgt_v != tgt_v) else int(round(tgt_v))  # NaN 判否
            cur = pos[s]
            o = O[i, s]
            o_nan = (o != o)
            # 强制平仓（窗口末或信号变 0/反向）：无论当日 open 是否缺失都重置仓位，
            # 仅 open 有效时记成交（避免旧仓位滞留导致漏记/错记）
            if cur != 0 and (is_last or is_rebal) and tgt != cur:
                if (not o_nan) and e_i[s] >= 0:
                    dirn = 1.0 if cur == 1 else -1.0
                    pnl = dirn * (o - e_px[s])
                    risk = sl_mult * e_atr[s]
                    if risk > 0:
                        cost_pts = e_cost[s]
                        net = pnl - cost_pts
                        trades.append({
                            "symbol": syms[s],
                            "side": "LONG" if cur == 1 else "SHORT",
                            "entry_dt": dates[e_i[s]], "exit_dt": dates[i],
                            "entry_px": e_px[s], "exit_px": o,
                            "pnl": pnl, "cost_pts": cost_pts, "risk": risk,
                            "gross": pnl / risk, "cost": cost_pts / risk,
                            "net": net / risk,
                            "holding_days": (dates[i] - dates[e_i[s]]).days,
                        })
                pos[s] = 0
                e_i[s] = -1
            # 开仓（rebalance 日且 target 非零且与当前不同；避免末日开仓）
            if tgt != 0 and tgt != cur and is_rebal and not is_last:
                a = A[i, s]
                if (not o_nan) and (a == a) and a > 0:
                    if contract_map is None:
                        ctr = main_contract_at(_variety(syms[s]), di)
                    else:
                        ctr = contract_map.get((syms[s], di))
                    key = (syms[s], ctr)
                    coef = coef_cache.get(key) if coef_cache is not None else None
                    if coef is None:
                        try:
                            coef = cost_coefficients(syms[s], contract=ctr,
                                                    close_action="CLOSE_YEST",
                                                    on_date=di)
                        except Exception:
                            coef = None
                        if coef_cache is not None and coef is not None:
                            coef_cache[key] = coef
                    cp = cost_points_at(coef, o) if coef is not None else 0.0
                    pos[s] = tgt
                    e_px[s] = o
                    e_atr[s] = a
                    e_cost[s] = cp
                    e_i[s] = i
    return trades


# ------------------------------------------------------------------- 安慰剂
def permute_targets(targets: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """标签置换安慰剂：每个 rebalance 日，保持「多头数/空头数」不变，但随机重选品种。
    破坏信号的选择性，同时保留暴露数量结构 —— 与 §8.8 的「随机同结构子集」逻辑一致。"""
    out = targets.copy()
    cols = list(targets.columns)
    ncol = len(cols)
    for d in targets.index:
        row = targets.loc[d].to_numpy(dtype=float)
        longs = [j for j, v in enumerate(row) if v > 0]
        shorts = [j for j, v in enumerate(row) if v < 0]
        nL, nS = len(longs), len(shorts)
        if nL == 0 and nS == 0:
            continue
        perm = rng.permutation(ncol)
        new = np.zeros(ncol, dtype=float)
        if nL:
            new[perm[:nL]] = 1.0
        if nS:
            new[perm[nL:nL + nS]] = -1.0
        out.loc[d, cols] = new
    return out


def simulate_windows(targets, opens, closes, atr, wfp, start, end,
                     rebalance_n, sl_mult, contract_map=None,
                     coef_cache=None) -> list[dict]:
    """与 actual 一致的窗口模拟：逐 WF 窗切片（含 warmup 历史），只收 entry>=te_s 成交。"""
    all_trades: list[dict] = []
    for tr_s, tr_e, te_s, te_e in _windows(start, end, wfp):
        mask = ((targets.index >= (te_s - timedelta(days=wfp.warmup_days))) &
                (targets.index <= te_e))
        if not mask.any():
            continue
        tr = simulate(targets[mask], opens[mask], closes[mask], atr[mask],
                     rebalance_n, sl_mult, contract_map=contract_map,
                     coef_cache=coef_cache)
        for t in tr:
            if _dt(t["entry_dt"]) is not None and _dt(t["entry_dt"]).date() >= te_s:
                all_trades.append(t)
    return all_trades


def placebo_agg(targets, opens, closes, atr, wfp, start, end, rebalance_n,
                sl_mult, actual_agg, n_perm=300, seed=20261101,
                contract_map=None, coef_cache=None):
    """标签置换安慰剂：与 actual 用**完全相同的窗口模拟逻辑**（simulate_windows），
    仅把每 rebalance 日的多/空品种随机重选（保持暴露数量），破坏信号选择性。
    pl_ge% = 置换后 agg_net >= 实际 agg_net 的比例（越低越真）。"""
    rng = np.random.default_rng(seed)
    ge = 0
    counted = 0
    for _ in range(n_perm):
        pt = permute_targets(targets, rng)
        tr = simulate_windows(pt, opens, closes, atr, wfp, start, end,
                              rebalance_n, sl_mult, contract_map=contract_map,
                              coef_cache=coef_cache)
        s = seg_stats(tr)
        if s.get("n", 0) == 0:
            continue
        counted += 1
        if s["sum_net_R"] >= actual_agg:
            ge += 1
    return round(ge / counted * 100, 1) if counted else float("nan")


# ------------------------------------------------------------------- 编排
def run_signal_wf(signal_fn, signal_name: str, symbols: list[str],
                  start: date, end: date, rebalance_n: int = REBALANCE_N,
                  sl_mult: float = SL_MULT, out: str | None = None,
                  do_placebo: bool = True, n_perm: int = 300,
                  **sig_kwargs) -> dict:
    """端到端：加载行情 → 计算 targets（全历史一次）→ 逐 WF 窗模拟 → 聚合 + 安慰剂。"""
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    # 数据起点前推 400 天，保证首个窗口 warmup/信号 trailing 窗口有历史
    data_start = start - timedelta(days=400)
    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    atr = daily_atr(highs, lows, closes)
    # 预建主力合约面（每品种一条 roll SQL），使 simulate 内循环零 DB 调用
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    session.close(); engine.dispose()

    targets = signal_fn(closes, opens=opens, **sig_kwargs)
    targets = targets.reindex(index=opens.index, columns=symbols).fillna(0.0)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    all_trades = simulate_windows(targets, opens, closes, atr, wfp, start, end,
                                  rebalance_n, sl_mult, contract_map=contract_map,
                                  coef_cache=coef_cache)
    n_windows = len(list(_windows(start, end, wfp)))

    rows = all_trades
    overall = seg_stats(rows)
    by_year: dict = defaultdict(list)
    for r in rows:
        by_year[_dt(r["entry_dt"]).year].append(r)
    by_year_stats = {str(y): seg_stats(v) for y, v in sorted(by_year.items())}

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
    if rows and do_placebo:
        actual_agg = overall["sum_net_R"]
        report["placebo"] = {
            "rand_same_structure_ge_actual_pct": placebo_agg(
                targets, opens, closes, atr, wfp, start, end, rebalance_n,
                sl_mult, actual_agg, n_perm=n_perm,
                contract_map=contract_map, coef_cache=coef_cache),
            "n_perm": n_perm,
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
        p = report["placebo"]
        print(f"  安慰剂 rand_ge_actual% = {p['rand_same_structure_ge_actual_pct']} "
              f"(n_perm={p.get('n_perm')})")


if __name__ == "__main__":
    print("此模块为底座库，由 signal_b1_momentum.py / signal_b3_pairs.py 调用。")
