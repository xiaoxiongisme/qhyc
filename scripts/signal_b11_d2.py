#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""十二期① / D2：bar 切块网格的入口断言 + 单日污染的反事实修复

背景（§32 D2）：
  l1_mkt.bar_60m 存在两套互斥的切块网格——
    S 会话网格（正常）：每 **60 个交易分钟**一根、跨休市继续累计、标签 = **块结束时刻**
                       日盘 10:00 / 11:15 / 14:15 / 15:00，夜盘 22:00 / 23:00。
                       与 app/ingest/synthesizer.py::_SESSION_DAY_BOUNDS[60]=[60,135,315,360] 同源。
    W 自然整点网格 ：墙钟整点切块、标签 = **块开始时刻**、残段并入整点桶、
                       并产生零成交边界桶（15:00 / 23:00）。RB888 一天 9 根。
  2026-11-02 全市场 50 品种为 W 网格（由已停用的 time_bucket 路径写入），前后均为 S 网格。

本脚本做四件事：
  A 全期网格扫描 —— 证明"污染只有 2026-11-02 这 1 天"，并给出检测器的对照基率
  B 断言行为验证 —— raise / drop / warn 三策略在三个窗口上的实际行为；并证明
                   默认 warn 与不检查（off）**逐位一致**（不影响既有研究可复现性）
  C 反事实修复   —— 由 minute_bar 按会话规则重建 11-02 的 60m，量化三层影响：
                   C2 日线层（应为 0，因为两套网格覆盖同一批分钟）
                   C3 60m 特征层（用**生产同一套** fusion 状态机 walk_fusion_states，
                      统计 state 翻转根数与开平仓翻转笔数）
  D 生产接入点   —— 打印应加断言的确切位置

用法：
  python signal_b11_d2.py                    # A+B+C2+C3
  python signal_b11_d2.py --with-b3          # 追加 C4（B3 全链 warn vs drop，较慢）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings

import signal_oos_backtest as sob
from signal_oos_backtest import (GRID_SESSION, GRID_WALLCLOCK, BarGridError,
                                 detect_bar_grid, load_market)

#: 会话网格日盘累计分钟边界（与 synthesizer._SESSION_DAY_BOUNDS[60] 逐位一致）
DAY_BOUNDS_MIN = (60, 135, 315, 360)
#: 分钟戳记「终点」约定的数据源（akshare / wh9999 记 HH:01…HH:00），需先减 1 分钟
END_LABELED_SRCS = ("akshare_1min", "wh9999_1m")


# ------------------------------------------------------------------ 工具
def _session():
    eng = create_engine(get_settings().db_url, pool_pre_ping=True)
    return sessionmaker(bind=eng)()


def _symbols_888(session) -> list[str]:
    rows = session.execute(text(
        "SELECT DISTINCT symbol FROM l1_mkt.bar_60m WHERE symbol LIKE '%888' ORDER BY 1")).all()
    return [r[0] for r in rows]


def frames_equal(a: tuple, b: tuple) -> bool:
    """四帧逐位相等（NaN 视为相等）。"""
    for x, y in zip(a, b):
        if not x.index.equals(y.index) or list(x.columns) != list(y.columns):
            return False
        xa = np.nan_to_num(x.to_numpy(float), nan=-9.87654321e12)
        ya = np.nan_to_num(y.to_numpy(float), nan=-9.87654321e12)
        if not np.array_equal(xa, ya):
            return False
    return True


def snap_day_label(minute_end: pd.Timestamp) -> pd.Timestamp:
    """日盘分钟 → 会话网格块标签（块结束时刻，按累计分钟边界吸附）。"""
    base = minute_end.normalize() + pd.Timedelta(hours=9)
    rel = (minute_end - base) / pd.Timedelta(minutes=1)
    for b in DAY_BOUNDS_MIN:
        if rel <= b:
            return base + pd.Timedelta(minutes=b)
    return base + pd.Timedelta(minutes=DAY_BOUNDS_MIN[-1])


def rebuild_session_60m(session, symbols: list[str], d: date) -> pd.DataFrame:
    """由 minute_bar 按会话规则重建某日 60m（标签 = 块结束时刻）。

    日盘：按 DAY_BOUNDS_MIN 吸附（等价于"每 60 交易分钟一根、跨休市累计"）。
    夜盘：从 21:00 起按 60 分钟向上取整，末段截断到该连续段最后一分钟 +1min。
    分钟戳先按 src 约定归一到「分钟起点」。
    """
    rows = session.execute(text("""
        SELECT symbol, ts, src, open, high, low, close, volume
        FROM l0_raw.minute_bar
        WHERE symbol = ANY(:syms) AND ts >= :s AND ts < :e
    """), {"syms": list(symbols), "s": pd.Timestamp(d), "e": pd.Timestamp(d) + timedelta(days=1)}).all()
    df = pd.DataFrame(rows, columns=["symbol", "ts", "src", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    end_src = df["src"].isin(END_LABELED_SRCS)
    df["ts_n"] = df["ts"] - pd.to_timedelta(np.where(end_src, 1, 0), unit="m")
    df = df.sort_values(["symbol", "ts_n"])
    df["m_end"] = df["ts_n"] + pd.Timedelta(minutes=1)
    hour = df["m_end"].dt.hour
    is_day = ((hour >= 9) & (hour <= 15)).to_numpy()
    # 日盘：吸附到会话边界；夜盘：从当日 21:00 起 60 分钟向上取整
    day_lab = df["m_end"].map(snap_day_label)
    base_night = df["m_end"].dt.normalize() + pd.Timedelta(hours=21)
    # 凌晨段（00:00–02:30 的夜盘尾巴）归属**前一日 21:00**，否则会被算成负偏移
    base_night = base_night.where(df["m_end"].dt.hour >= 12,
                                  base_night - pd.Timedelta(days=1))
    k = np.ceil(((df["m_end"] - base_night) / pd.Timedelta(minutes=1)) / 60.0)
    night_lab = base_night + pd.to_timedelta(k * 60, unit="m")
    # 夜盘末段截断：末根不足 60 分钟时压到「连续段最后一分钟 +1min」。
    # 只对夜盘做：日盘标签由固定边界吸附，不随末分钟收缩（否则 11:15 会被压成 11:11）。
    nt = pd.DataFrame({"symbol": df["symbol"].to_numpy(),
                       "lab": night_lab.to_numpy(), "m_end": df["m_end"].to_numpy()})
    # 截断基准必须是「该连续段最后一分钟」，不是本行自己的 m_end（否则 21:01 会被压成 21:01）
    seg_last = nt.groupby(["symbol", "lab"])["m_end"].transform("max")
    nt["lab"] = pd.concat([nt["lab"], seg_last], axis=1).min(axis=1)
    df["label"] = day_lab.to_numpy()
    df.loc[~is_day, "label"] = nt.loc[~is_day, "lab"].to_numpy()
    out = df.groupby(["symbol", "label"], as_index=False).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    return out.rename(columns={"label": "dt"})


def load_native_60m(session, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    rows = session.execute(text("""
        SELECT symbol, bucket, open, high, low, close, volume FROM l1_mkt.bar_60m
        WHERE symbol = ANY(:syms) AND bucket >= :s AND bucket < :e
    """), {"syms": list(symbols), "s": pd.Timestamp(start), "e": pd.Timestamp(end)}).all()
    df = pd.DataFrame(rows, columns=["symbol", "dt", "open", "high", "low", "close", "volume"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df.sort_values(["symbol", "dt"]).reset_index(drop=True)


# ------------------------------------------------------------------ A
def section_a(session, symbols, start, end) -> dict:
    print("\n[A] 全期网格扫描")
    t0 = time.time()
    rep = detect_bar_grid(session, start, end, symbols=symbols)
    print(f"   区间 {start}..{end}  n_days={rep['n_days']}  grids={rep['grids']}  "
          f"mixed={rep['mixed']}  bad_days={rep['n_bad']}  [{time.time()-t0:.0f}s]")
    for d in rep["bad_days"]:
        c = rep["by_day"][d]
        print(f"   ✗ {d}  session={c[GRID_SESSION]} wallclock={c[GRID_WALLCLOCK]} "
              f"unknown={c['unknown']}")
    if rep["offenders"]:
        print(f"   判决为 session 但混入非 session 的 (日期,品种) 共 {len(rep['offenders'])} 条，"
              f"前 5：{rep['offenders'][:5]}")
    # 反面基率：整点网格独有标签在正常日的出现率
    r2 = session.execute(text("""
        SELECT count(*) FILTER (WHERE lbl IN ('11:15','14:15')) n_s,
               count(*) FILTER (WHERE lbl IN ('09:00','11:00','13:00')) n_w, count(*) n
        FROM (SELECT to_char(bucket,'HH24:MI') lbl FROM l1_mkt.bar_60m
              WHERE bucket >= :s AND bucket <= :e
                AND extract(hour from bucket) BETWEEN 9 AND 15) x
    """), {"s": pd.Timestamp(start), "e": pd.Timestamp(end)}).one()
    print(f"   日盘标签基数：会话独有 {r2[0]:,} / 整点独有 {r2[1]:,} / 合计 {r2[2]:,}")
    return {"range": [str(start), str(end)], "n_days": rep["n_days"],
            "grids": rep["grids"], "mixed": rep["mixed"],
            "bad_days": [str(d) for d in rep["bad_days"]],
            "bad_day_detail": {str(d): rep["by_day"][d] for d in rep["bad_days"]},
            "n_offenders": len(rep["offenders"]),
            "label_base_rates": {"session_sig": int(r2[0]), "wall_sig": int(r2[1]),
                                 "total": int(r2[2])}}


# ------------------------------------------------------------------ B
def section_b(session, symbols, windows: dict, focus: date) -> dict:
    print("\n[B] 入口断言行为验证")
    out = {}
    for name, (s, e) in windows.items():
        rep = detect_bar_grid(session, s, e, symbols=symbols)
        row = {"window": [str(s), str(e)], "n_bad": rep["n_bad"],
               "bad_days": [str(d) for d in rep["bad_days"]][:5]}
        # raise
        t0 = time.time()
        try:
            load_market(session, symbols, s, e, calendar_mode="weekend_drop",
                        grid_policy="raise")
            row["raise"] = "passed(no exception)"
        except BarGridError as ex:
            row["raise"] = f"raised: {str(ex)[:90]}"
        row["raise_sec"] = round(time.time() - t0, 1)
        out[name] = row
        print(f"   {name:<6} {s}..{e}  bad={rep['n_bad']}  raise → {row['raise'][:60]}")

    # 默认 warn 与 off 逐位一致
    s, e = windows["W_bad"]
    t0 = time.time()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        f_warn = load_market(session, symbols, s, e, calendar_mode="weekend_drop",
                             grid_policy="warn")
        n_warn = len(w)
    f_off = load_market(session, symbols, s, e, calendar_mode="weekend_drop",
                        grid_policy="off")
    identical = frames_equal(f_warn, f_off)
    msg = str(w[0].message)[:150] if n_warn else ""
    print(f"   warn vs off 逐位一致 = {identical}（warn 触发 {n_warn} 次）"
          f"  首条：{msg[:100]}")
    print(f"   耗时 {time.time()-t0:.1f}s（含两次全区间取数）")
    out["warn_vs_off"] = {"identical": bool(identical), "n_warn": n_warn, "message": msg}

    # drop 真的剔掉污染日
    f_drop = load_market(session, symbols, s, e, calendar_mode="weekend_drop",
                         grid_policy="drop")
    rep_drop = sob.LAST_GRID_REPORT
    days_drop = set(f_drop[1].index)
    print(f"   drop: dropped_rows={rep_drop.get('dropped_rows')}  "
          f"{focus} 是否仍在索引 = {focus in days_drop}")
    out["drop"] = {"dropped_rows": rep_drop.get("dropped_rows"),
                   "focus_day_removed": focus not in days_drop,
                   "n_days_warn": len(f_warn[1].index), "n_days_drop": len(f_drop[1].index)}
    return out


# ------------------------------------------------------------------ C2
def section_c2(session, native: pd.DataFrame, rebuilt: pd.DataFrame, focus: date) -> dict:
    print("\n[C2] 反事实修复：日线层影响（预期 0）")
    d0 = pd.Timestamp(focus)
    n = native[(native["dt"].dt.date == focus) & (native["dt"].dt.hour.between(9, 15))]
    r = rebuilt[(rebuilt["dt"].dt.date == focus) & (rebuilt["dt"].dt.hour.between(9, 15))]
    print(f"   日盘根数：native={len(n.groupby('symbol'))}  rebuilt={len(r.groupby('symbol'))}"
          f"（每品种）")
    labs_n = sorted(n["dt"].dt.strftime("%H:%M").unique())
    labs_r = sorted(r["dt"].dt.strftime("%H:%M").unique())
    print(f"   native 标签={labs_n}")
    print(f"   rebuilt标签={labs_r}")

    def day_ohlc(df):
        x = df.sort_values("dt")
        g = x.groupby("symbol")
        return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                             "low": g["low"].min(), "close": g["close"].last(),
                             "n": g.size()})

    a, b = day_ohlc(n), day_ohlc(r)
    j = a.join(b, lsuffix="_n", rsuffix="_r", how="inner")
    for c in ("open", "high", "low", "close"):
        j[f"d_{c}"] = (j[f"{c}_r"] / j[f"{c}_n"] - 1).abs()
    worst = float(max(j["d_open"].max(), j["d_high"].max(), j["d_low"].max(), j["d_close"].max()))
    n_diff = int(((j[["d_open", "d_high", "d_low", "d_close"]] > 1e-9).any(axis=1)).sum())
    print(f"   日盘 OHLC 相对偏差 max={worst:.2e}  有差异品种={n_diff}/{len(j)}")
    ex = "RB888"
    if ex in j.index:
        x = j.loc[ex]
        print(f"   {ex}: native O={x['open_n']} H={x['high_n']} L={x['low_n']} C={x['close_n']} n={x['n_n']}")
        print(f"   {ex}: rebuilt O={x['open_r']} H={x['high_r']} L={x['low_r']} C={x['close_r']} n={x['n_r']}")
    return {"n_bars_per_symbol_native": int(a["n"].iloc[0]) if len(a) else 0,
            "n_bars_per_symbol_rebuilt": int(b["n"].iloc[0]) if len(b) else 0,
            "labels_native": labs_n, "labels_rebuilt": labs_r,
            "day_ohlc_max_rel_diff": worst, "n_symbols_ohlc_diff": n_diff,
            "n_symbols": int(len(j)),
            "verdict": "日线层无影响（两套网格覆盖同一批分钟，首末/极值等价）"
                       if worst <= 1e-9 else "日线层受影响"}


# ------------------------------------------------------------------ C3
def section_c3(session, native: pd.DataFrame, rebuilt: pd.DataFrame,
               symbols: list[str], focus: date, days: int = 60) -> dict:
    print("\n[C3] 反事实修复：60m 特征层（生产 fusion 状态机）")
    from app.backtest.fusion_backtest import _generate_states, fusion_params_from_config
    p = fusion_params_from_config()
    lo = pd.Timestamp(focus, tz="Asia/Shanghai") - timedelta(days=days)
    hi = pd.Timestamp(focus, tz="Asia/Shanghai") + timedelta(days=days)
    d0 = pd.Timestamp(focus).date()

    flipped, feat, per_sym = [], [], []
    for sym in symbols:
        a = native[(native["symbol"] == sym) & (native["dt"] >= lo) & (native["dt"] < hi)]
        if len(a) < p.min_bars:
            continue
        r = rebuilt[(rebuilt["symbol"] == sym) & (rebuilt["dt"] >= lo) & (rebuilt["dt"] < hi)]
        # 反事实序列：把污染日（含零成交边界桶）整段换成会话网格重建值
        fixed = pd.concat([a[a["dt"].dt.date != d0], r], ignore_index=True)
        fixed = fixed.sort_values("dt").reset_index(drop=True)
        aa = a.sort_values("dt").reset_index(drop=True)
        try:
            sa = _generate_states(aa[["open", "high", "low", "close"]].copy(), p)
            sb = _generate_states(fixed[["open", "high", "low", "close"]].copy(), p)
        except Exception as ex:  # noqa: BLE001
            per_sym.append({"symbol": sym, "error": f"{type(ex).__name__}: {ex}"})
            continue
        dt_a = aa["dt"].reset_index(drop=True)
        dt_b = fixed["dt"].reset_index(drop=True)
        st_a = pd.Series(sa["state"].to_numpy(), index=dt_a)
        st_b = pd.Series(sb["state"].to_numpy(), index=dt_b)
        atr_a = pd.Series(sa["atr"].to_numpy(float), index=dt_a)
        atr_b = pd.Series(sb["atr"].to_numpy(float), index=dt_b)
        ema_a = pd.Series(aa["close"].to_numpy(float), index=dt_a).ewm(
            span=p.ema_k, adjust=False).mean()
        ema_b = pd.Series(fixed["close"].to_numpy(float), index=dt_b).ewm(
            span=p.ema_k, adjust=False).mean()

        # 两套网格根数不同（9 vs 6），只能按时间戳取交集比较，不能按位置比
        common = dt_a[dt_a.isin(st_b.index)].reset_index(drop=True)
        diff_bars = int((st_a.reindex(common) != st_b.reindex(common)).sum())

        def trans(states, dts):
            # dts 是经过布尔过滤的 Series，索引有断层 → 必须转 ndarray 再按下标取
            dates = dts.to_numpy()
            ss = states.reindex(dts).to_numpy()
            return {dates[i] for i in range(1, len(ss)) if ss[i] != ss[i - 1]}
        n_entry_flip = len(trans(st_a, common) ^ trans(st_b, common))

        post = common[common.map(lambda t: t.date() >= d0)]
        if len(post):
            d_atr = (atr_b.reindex(post) / atr_a.reindex(post) - 1).abs().max()
            d_ema = (ema_b.reindex(post) / ema_a.reindex(post) - 1).abs().max()
            feat.append({"symbol": sym,
                         "atr_rel": float(d_atr) if pd.notna(d_atr) else 0.0,
                         "ema_rel": float(d_ema) if pd.notna(d_ema) else 0.0})
        firstd = [t for t in common if st_a.get(t, 0) != st_b.get(t, 0)]
        lastd = firstd[-1] if firstd else None
        # 破坏外溢到污染日之后的 bar 数（衡量影响会否延续到后续交易日）
        persist = sum(1 for t in firstd if t.date() > d0)
        per_sym.append({"symbol": sym, "n_bars": len(aa), "diff_bars": diff_bars,
                        "n_transition_flip": n_entry_flip,
                        "first_diff": str(firstd[0]) if firstd else "",
                        "last_diff": str(lastd) if lastd else "",
                        "diff_bars_after_focus": persist})
        if diff_bars or n_entry_flip:
            flipped.append(per_sym[-1])

    n_sym = len(per_sym)
    n_state = sum(1 for x in per_sym if x.get("diff_bars"))
    n_enter = sum(1 for x in per_sym if x.get("n_transition_flip"))
    atr_max = max((f["atr_rel"] for f in feat), default=0.0)
    ema_max = max((f["ema_rel"] for f in feat), default=0.0)
    atr_med = float(np.median([f["atr_rel"] for f in feat])) if feat else 0.0
    print(f"   样本品种={n_sym}  state 有差异的品种={n_state}  "
          f"开平仓时刻有翻转的品种={n_enter}")
    print(f"   ATR14 相对差：中位={atr_med:.2e}  max={atr_max:.2e}；"
          f"EMA{p.ema_k} 相对差 max={ema_max:.2e}")
    for x in flipped[:8]:
        print(f"   · {x['symbol']}: diff_bars={x['diff_bars']} "
              f"transition_flip={x['n_transition_flip']} first={x['first_diff']}")
    return {"n_symbols": n_sym, "n_symbols_state_diff": n_state,
            "n_symbols_entry_flip": n_enter,
            "atr_rel_median": atr_med, "atr_rel_max": atr_max, "ema_rel_max": ema_max,
            "flipped": flipped[:20], "per_symbol": per_sym}


# ------------------------------------------------------------------ C4
def section_c4(session, symbols, start, end, focus) -> dict:
    print("\n[C4] B3 全链：warn vs drop（污染日剔除的影响）")
    from signal_oos_backtest import WalkForwardParams, daily_atr, main_contract_surface, simulate_windows
    from signal_b3_pairs import compute_targets
    from signal_b3_compound_oos import PAIRS_ALL
    from signal_b3_riskfix import SL_MULT
    from signal_b10_caliber import summarize

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    runs = {}
    for pol in ("warn", "drop"):
        t0 = time.time()
        o, c, h, l = load_market(session, symbols, start - timedelta(days=400), end,
                                 calendar_mode="weekend_drop", grid_policy=pol)
        atr = daily_atr(h, l, c)
        cmap = main_contract_surface(session, symbols, list(o.index))
        tgt, _ = compute_targets(c, pairs=PAIRS_ALL, window=60,
                                 entry_z=2.0, exit_z=0.5, return_strength=True)
        tt = tgt.reindex(index=o.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(tt, o, c, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=cmap, coef_cache={})
        s = summarize(trades)
        on_focus = [x for x in trades if str(x["entry_dt"])[:10] == str(focus)]
        s["n_entry_on_focus"] = len(on_focus)
        s["sec"] = round(time.time() - t0, 1)
        runs[pol] = s
        print(f"   {pol}: n={s['n']} agg_net={s['agg_net']} mean={s['mean']} "
              f"win={s['win']:.3f}  当日开仓={s['n_entry_on_focus']}")
    d = {"n_trades": runs["drop"]["n"] - runs["warn"]["n"],
         "agg_net": round(runs["drop"]["agg_net"] - runs["warn"]["agg_net"], 6)}
    print(f"   Δ: trades={d['n_trades']:+d} agg_net={d['agg_net']:+.6f}")
    return {"runs": runs, "delta": d}


# ------------------------------------------------------------------ D
def section_d() -> dict:
    print("\n[D] 生产接入点（仅供决策，本脚本不修改生产代码）")
    pts = [
        ("app/backtest/fusion_backtest.py::_read_hourly", "bar_60m 分支",
         "回测侧唯一 60m 入口；在此对返回帧做一次网格校验即可覆盖全部回测"),
        ("app/strategies/fusion_signal.py::run/walk", "实时侧",
         "实时走 cfg.hourly_src=akshare→l0_raw.hourly_bar；实测其 2026-11-02 为正常会话网格"
         "（10:00/11:15/14:15/15:00），故当日实盘信号未被污染"),
        ("app/ingest/synthesizer.py::_synth_one", "写入侧",
         "_INSERT_HEAD（自然整点桶）已停用但代码仍在；建议删除或加 import 期断言，"
         "防止回归"),
        ("scripts/signal_oos_backtest.py::load_market", "研究侧",
         "本次已加 grid_policy='raise'|'drop'|'warn'|'off'"),
    ]
    for a, b, c in pts:
        print(f"   · {a} [{b}]\n     {c}")
    return {"points": [{"where": a, "side": b, "note": c} for a, b, c in pts]}


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-11-03")
    ap.add_argument("--focus", default="2026-11-02")
    ap.add_argument("--with-b3", action="store_true")
    ap.add_argument("--out", default="/tmp/aeq/b11_d2.json")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    focus = date.fromisoformat(args.focus)
    session = _session()
    symbols = _symbols_888(session)
    print(f"[init] symbols={len(symbols)}  range={start}..{end}  focus={focus}")

    out = {"meta": {"range": [str(start), str(end)], "focus": str(focus),
                    "n_symbols": len(symbols),
                    "run_at": datetime.now().isoformat(timespec="seconds")}}

    out["A_scan"] = section_a(session, symbols, start, end)

    # load_market 的端点是 `bucket <= end`（end=00:00），故 end 取「末日的次日」才能纳入末日
    # 整日数据；该语义既有，本次不改进以免改变历史研究结果。
    windows = {"W_ok": (date(2021, 1, 1), date(2026, 10, 31)),
               "W_bad": (date(2021, 1, 1), focus + timedelta(days=1)),
               "W_only": (focus - timedelta(days=1), focus + timedelta(days=1))}
    out["B_assert"] = section_b(session, symbols, windows, focus)

    # ---- C 反事实修复 ----
    print("\n[C1] 由 minute_bar 重建污染日的会话网格")
    t0 = time.time()
    rebuilt = rebuild_session_60m(session, symbols, focus)
    print(f"   重建行数={len(rebuilt):,}  品种={rebuilt['symbol'].nunique()}  [{time.time()-t0:.0f}s]")
    native = load_native_60m(session, symbols, focus - timedelta(days=120), focus + timedelta(days=120))
    nat_focus = native[native["dt"].dt.date == focus]
    print(f"   污染日 native 行数={len(nat_focus):,}  "
          f"每品种根数中位={int(nat_focus.groupby('symbol').size().median())}；"
          f"重建后中位={int(rebuilt.groupby('symbol').size().median())}")
    out["C1_rebuild"] = {"rebuilt_rows": int(len(rebuilt)),
                         "native_rows_on_focus": int(len(nat_focus)),
                         "native_bars_per_symbol_median": int(nat_focus.groupby("symbol").size().median()),
                         "rebuilt_bars_per_symbol_median": int(rebuilt.groupby("symbol").size().median())}

    out["C2_daily"] = section_c2(session, native, rebuilt, focus)
    out["C3_features"] = section_c3(session, native, rebuilt, symbols, focus)

    if args.with_b3:
        out["C4_b3"] = section_c4(session, symbols, date(2024, 1, 1), focus, focus)
    out["D_wiring"] = section_d()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
    print(f"\n[done] → {args.out}")


if __name__ == "__main__":
    main()
