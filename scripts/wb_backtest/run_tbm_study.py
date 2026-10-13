"""
Prado 三重障碍法(Triple Barrier Method, TBM) 与融合策略 V3.4 的结合回测
=========================================================================

设计目标（严格 A/B，只看"离场方法"差异）：
- 入场：完全复用融合策略 V3.4 信号层 walk_fusion_states（无前视，单一真源）。
  入场事件 = 基准回测里每笔成交的 (入场 bar / 方向 / 入场价 / 入场 ATR)。
  —— 基准与全部 TBM 配置共用**同一入场流**，因此入场口径逐位一致，差异 100% 来自离场。
- 基准离场：融合原生（2×ATR 初始 + 2×ATR 吊灯 + 0.5R 保本，仅收盘确认）。
- TBM 离场：三道屏障首触即平
    · 上水平（盈利）屏障  TP = entry ± PT × ATR(entry)
    · 下水平（止损）屏障  SL = entry ∓ SL × ATR(entry)   （SL 固定 2.0，与基准初始止损同量级）
    · 垂直（时间）屏障    N 根后强制以当根收盘平仓
  三道中**最先被触及**者决定离场价（首触即平，使用当根 high/low 判定，无前视）。
  同根同时触 TP/SL：取"距当根开盘更近"的那道先触发（更贴近真实路径）。

为避免加码混淆，基准与 TBM 均固定 add_max_lots=1（无金字塔）。

⚠ 参数非数据挖出：PT∈{1.5,2,3}、N∈{20,40,80}、SL=2.0 均为 Prado 从业者默认值，
  不对本数据做最优选择后报"最佳"——结论只看**定性方向**（加固定止盈/时间止损
  对融合策略风险收益轮廓的影响），规避多重比较假阳性（CB 纪律 #2/#10）。

指标口径与 fusion_backtest._metrics_from_trades 完全一致（逐笔 pnl_pct 几何复利累计 +
最大回撤 + 近似夏普），保证基准/TBM 可比。另增：平均持仓根数、平均 R 倍数。
"""
from __future__ import annotations
import sys, os, json, time
from datetime import date
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# —— 复用融合回测信号层与指标（单一真源）——
sys.path.insert(0, "/Docker/qhyc")
from app.backtest.fusion_backtest import (
    fusion_params_from_config, _read_hourly, _generate_states,
    _atr, _simulate_trades, _metrics_from_trades, FusionBacktestParams,
)
from app.core.db import session_scope

# —— 成本口径（与融合成交一致：按建仓日历史费率，真乘数）——
from app.data.cost import (cost_coefficients, cost_points_at,
                           main_contract_at, resolve_rate_date, _variety as _vc)


def _symbol_mult(symbol):
    try:
        return float(cost_coefficients(symbol, close_action="CLOSE_YEST")["multiplier"])
    except Exception:
        return 1.0


def _cost_pts(symbol, entry_dt, entry_px):
    """按建仓日历史费率算开平成本（点数）。与 _simulate_trades._coef_for 同口径。"""
    d0 = entry_dt.date() if hasattr(entry_dt, "date") else entry_dt
    ctr = main_contract_at(_vc(symbol), d0)
    coef = cost_coefficients(symbol, contract=ctr, close_action="CLOSE_YEST", on_date=d0)
    return cost_points_at(coef, entry_px)


def extract_entries_from_baseline(trades, dt_index):
    """从基准成交取入场事件（与基准 1:1 对应），dt→bar 下标。"""
    evs = []
    for t in trades:
        if t.get("open"):
            continue
        ei = dt_index.get(t["entry_dt"])
        if ei is None:
            continue
        evs.append({
            "sym": t["symbol"], "ei": ei,
            "dir": 1.0 if t["side"] == "LONG" else -1.0,
            "entry_px": float(t["entry_px"]),
            "atr_e": float(t.get("atr") or 0.0),
            "entry_dt": t["entry_dt"],
        })
    return evs


def simulate_tbm(ev, o, h, l, c, atr_arr, PT, SL, N):
    """对单笔入场事件跑 TBM 离场，返回 (exit_px, exit_i, hit_type)。"""
    ei = ev["ei"]; d = ev["dir"]; ep = ev["entry_px"]
    ae = ev["atr_e"] if ev["atr_e"] > 0 else float(atr_arr[ei])
    if ae <= 0:
        ae = float(np.nanmean(atr_arr)) or 1.0
    if d > 0:  # 多
        tp_px = ep + PT * ae
        sl_px = ep - SL * ae
    else:       # 空
        tp_px = ep - PT * ae
        sl_px = ep + SL * ae
    top = min(len(c) - 1, ei + N)
    for j in range(ei + 1, len(c)):
        hi = h[j]; lo = l[j]; op = o[j]
        if j > top:  # 垂直屏障：以时间上限根收盘平仓
            return float(c[j]), j, "vertical"
        if d > 0:
            hit_sl = lo <= sl_px
            hit_tp = hi >= tp_px
        else:
            hit_sl = hi >= sl_px
            hit_tp = lo <= tp_px
        if hit_sl and hit_tp:
            # 同根双触：取距开盘更近者先触发
            if abs(op - sl_px) <= abs(op - tp_px):
                return float(sl_px), j, "sl"
            else:
                return float(tp_px), j, "tp"
        elif hit_tp:
            return float(tp_px), j, "tp"
        elif hit_sl:
            return float(sl_px), j, "sl"
    # 走到序列末尾仍未触发：以最后收盘强平
    return float(c[-1]), len(c) - 1, "eod"


def build_tbm_trades(symbol, entries, o, h, l, c, atr_arr, PT, SL, N, mult):
    trades = []
    for ev in entries:
        xp, xj, htype = simulate_tbm(ev, o, h, l, c, atr_arr, PT, SL, N)
        d = ev["dir"]; ep = ev["entry_px"]
        gross = d * (xp - ep)
        cost_pts = _cost_pts(symbol, ev["entry_dt"], ep)
        pnl = gross - cost_pts
        risk = SL * ev["atr_e"] if ev["atr_e"] > 0 else 0.0
        R = float(d * (xp - ep) / risk) if risk > 0 else 0.0
        trades.append({
            "symbol": symbol, "side": "LONG" if d > 0 else "SHORT",
            "entry_dt": ev["entry_dt"], "exit_dt": None,
            "entry_px": round(ep, 4), "exit_px": round(xp, 4),
            "pnl": round(float(pnl), 4),
            "pnl_pct": round(float(pnl / ep * 100.0), 4) if ep else 0.0,
            "addon": False, "atr": float(ev["atr_e"]), "risk": risk,
            "R": R, "hit": htype, "hold_bars": xj - ev["ei"],
            "mult": mult, "cost_points": cost_pts,
        })
    return trades


def compute_rich(trades: list[dict]) -> dict:
    """期望值口径（与逐笔几何累计互补，专用于出场 A/B）：
    - mean_R / net_R：每笔以 initial-risk(=SL×ATR_entry) 归一，可跨品种直接相加；
    - profit_factor：总盈利R / |总亏损R|；
    - avg_win_R / avg_loss_R：盈亏不对称度（吊灯让利润奔跑→avg_win_R 显著>avg_loss_R）；
    - win_rate / avg_hold_bars。
    """
    closed = [t for t in trades if not t.get("open")]
    n = len(closed)
    if n == 0:
        return {"n": 0}
    Rs = np.array([float(t.get("R", 0.0)) for t in closed], dtype=float)
    wins = Rs[Rs > 0]; losses = Rs[Rs < 0]
    gp = float(wins.sum()) if wins.size else 0.0
    gl = float(losses.sum()) if losses.size else 0.0
    pf = (gp / abs(gl)) if gl < 0 else 99.0
    holds = [float(t.get("hold_bars", 0)) for t in closed if t.get("hold_bars") is not None]
    return {
        "n": n,
        "win_rate": round(float((Rs > 0).mean()), 4),
        "mean_R": round(float(Rs.mean()), 5),
        "net_R": round(float(Rs.sum()), 2),
        "profit_factor": round(pf, 3),
        "avg_win_R": round(float(wins.mean()), 4) if wins.size else 0.0,
        "avg_loss_R": round(float(losses.mean()), 4) if losses.size else 0.0,
        "avg_hold_bars": round(float(np.mean(holds)), 2) if holds else None,
    }


def _jsonable_trade(t):
    out = dict(t)
    for k in ("entry_dt", "exit_dt", "edt", "xdt"):
        v = out.get(k)
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


CACHE_PATH = "/tmp/tbm_cache.json"


def run_study(session, symbols, start, end, grid, cache_path=CACHE_PATH):
    base_p = fusion_params_from_config({"add_max_lots": 1})
    results = {}            # "PTx_Ny" -> list[TBM trades]
    per_sym_baseline = {}  # sym -> framework metrics
    base_all = []          # 基准全样本成交（带 hold_bars）
    skipped = []
    # 载入已有缓存（崩溃续跑）
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    for sym, d in cache.items():
        per_sym_baseline[sym] = d["baseline_metrics"]
        base_all.extend(d["baseline_trades"])
        for k, trs in d["tbm"].items():
            results.setdefault(k, []).extend(trs)
    # 1) 逐品种：一次算 states，基准 + 各 TBM 配置共用入场
    for sym in [s for s in symbols if s not in cache]:
        # ① 缺规格品种跳过（dim_variety 无 cost 系数 → 成本无法计算）
        try:
            mult = _symbol_mult(sym)
        except Exception as e:
            skipped.append(sym)
            print(f"[skip] {sym}: {str(e)[:50]}", flush=True)
            continue
        df = _read_hourly(session, sym, base_p.src, start, end)
        if df is None or len(df) < base_p.min_bars:
            continue
        states_df = _generate_states(df, base_p)
        try:
            trades_base = _simulate_trades(states_df, base_p, symbol=sym)
        except Exception as e:
            skipped.append(sym)
            print(f"[skip-trade] {sym}: {str(e)[:50]}", flush=True)
            continue
        if not trades_base:
            continue
        # 入场事件（与基准 1:1）
        dt_index = {d: i for i, d in enumerate(states_df["dt"])}
        entries = extract_entries_from_baseline(trades_base, dt_index)
        if not entries:
            continue
        o = states_df["open"].to_numpy(float)
        h = states_df["high"].to_numpy(float)
        l = states_df["low"].to_numpy(float)
        c = states_df["close"].to_numpy(float)
        atr_arr = states_df["atr"].to_numpy(float)
        # 基准成交补 hold_bars
        for t in trades_base:
            if not t.get("open") and t["entry_dt"] in dt_index and t["exit_dt"] in dt_index:
                t["hold_bars"] = dt_index[t["exit_dt"]] - dt_index[t["entry_dt"]]
        base_sym = [{**t} for t in trades_base if not t.get("open")]
        base_all.extend(base_sym)
        per_sym_baseline[sym] = _metrics_from_trades(trades_base)
        # 各 TBM 配置
        tbm_for_sym = {}
        for (PT, N) in grid:
            key = f"PT{PT}_N{N}"
            tt = build_tbm_trades(sym, entries, o, h, l, c, atr_arr, PT, SL_FIXED, N, mult)
            results.setdefault(key, []).extend(tt)
            tbm_for_sym[key] = [_jsonable_trade(t) for t in tt]
        # ② 落盘缓存（崩溃可续跑，避免全宇宙重算）
        cache[sym] = {
            "baseline_metrics": per_sym_baseline[sym],
            "baseline_trades": [_jsonable_trade(t) for t in base_sym],
            "tbm": tbm_for_sym,
        }
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    # 2) 聚合
    out = {}
    out["baseline"] = _metrics_from_trades(base_all)
    out["baseline"]["rich"] = compute_rich(base_all)
    out["baseline"]["per_symbol"] = per_sym_baseline
    out["tbm"] = {}
    for (PT, N) in grid:
        key = f"PT{PT}_N{N}"
        tt = results.get(key, [])
        agg = _metrics_from_trades(tt)
        agg["rich"] = compute_rich(tt)
        if tt:
            n = len(tt)
            agg["hit_tp"] = round(sum(1 for t in tt if t["hit"] == "tp") / n, 4)
            agg["hit_sl"] = round(sum(1 for t in tt if t["hit"] == "sl") / n, 4)
            agg["hit_vertical"] = round(sum(1 for t in tt if t["hit"] == "vertical") / n, 4)
            agg["hit_eod"] = round(sum(1 for t in tt if t["hit"] == "eod") / n, 4)
        out["tbm"][key] = agg
    out["skipped"] = skipped
    return out


SL_FIXED = 2.0

if __name__ == "__main__":
    start = date(2022, 1, 1)
    end = date(2026, 9, 30)
    grid = [(1.5, 20), (1.5, 40), (1.5, 80),
            (2.0, 20), (2.0, 40), (2.0, 80),
            (3.0, 20), (3.0, 40), (3.0, 80)]
    from app.core.config import get_settings
    syms = [s.symbol for s in get_settings().main_contracts]
    # 控时：全宇宙
    t0 = time.time()
    with session_scope() as s:
        res = run_study(s, syms, start, end, grid)
    print(f"# elapsed={time.time()-t0:.1f}s  universe={len(syms)}  window={start}..{end}  SL={SL_FIXED}")
    print(f"# skipped (缺 dim_variety 成本规格): {res.get('skipped')}")
    b = res["baseline"]; br = b["rich"]
    print("\n=== BASELINE (融合原生吊灯离场, add_max_lots=1) ===")
    print(f"n={b['n_trades']} win={b['win_rate']} tot%={b['total_return_pct']} "
          f"mdd%={b['max_drawdown_pct']} sharpe={b['sharpe']}")
    print(f"  [expectancy] mean_R={br['mean_R']} net_R={br['net_R']} PF={br['profit_factor']} "
          f"avgWinR={br['avg_win_R']} avgLossR={br['avg_loss_R']} avgHold={br['avg_hold_bars']}")
    print("\n=== TBM grid (PT × N 小时) | n | win | tot% | mdd% | meanR | netR | PF | avgWinR | avgLossR | avgHold | tp% | sl% | vert% ===")
    for k, a in res["tbm"].items():
        r = a["rich"]
        print(f"{k:10} | {a['n_trades']:4} | {a['win_rate']} | {a['total_return_pct']:8.2f} | "
              f"{a['max_drawdown_pct']:7.2f} | {r['mean_R']} | {r['net_R']} | {r['profit_factor']} | "
              f"{r['avg_win_R']} | {r['avg_loss_R']} | {r['avg_hold_bars']} | "
              f"{a.get('hit_tp','-')} | {a.get('hit_sl','-')} | {a.get('hit_vertical','-')}")
    # 落地 JSON 供后续分析
    with open("/tmp/tbm_study_result.json", "w") as f:
        json.dump(res, f, default=str)
    print("\n[written] /tmp/tbm_study_result.json")
