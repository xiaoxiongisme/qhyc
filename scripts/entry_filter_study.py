#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场过滤信号级研究（2026-11-01）：在融合策略既有入场流上打 KDJ/MACD 状态标签。

目的（纯过滤统计，不改动引擎，不动离场）：
  H1 KDJ 高位追多 / 低位追空（顺势方向上 KDJ 已超买/超卖）是否更差；
  H2 入场时 MACD 柱与方向相反（"金叉遇绿柱"的引擎侧镜像）是否更差；
  H3 MACD 柱深缩（|柱| < 20% × 20根内峰）是否更差（衰竭信号）；
  H4 MACD 柱顺向扩张 vs 收缩的分组差异；
  H5 A/B 支路（回踩 vs 突破）× 指标状态的交叉。

口径（与 app/backtest/fusion_backtest.py 逐位一致）：
  - 数据 l1_mkt.bar_60m（单一真源）；htf_dir = 小时 EMA140、前一根对齐（无前视）；
  - 成交：bar 收盘 state 0→1/2 当根收盘开仓，→0 当根收盘平仓（close-only）；
  - 收益单位：R = 方向×(exit−entry) / (sl_atr × ATR_entry)，毛收益；
  - KDJ(9,3,3)、MACD(12,26,9) 只用 ≤ entry bar 收盘数据（无前视）；
  - 只统计首仓（state 0→非0），忽略阶梯加码。

用法（qhyc-api 容器内）：
  python scripts/entry_filter_study.py [--max-symbols N] [--out /tmp/entry_filter.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import (
    fusion_params_from_config, _read_hourly, _htf_direction,
)
from app.strategies.fusion_signal import walk_fusion_states

IS_END = pd.Timestamp("2021-01-01", tz="Asia/Shanghai")  # IS: < 此日期；OOS: >= 此日期


def kdj93(h, l, c, n=9):
    """KDJ(9,3,3)：RSV 3 点 Wilder 平滑（国内行情软件标准口径）。"""
    h = pd.Series(h)
    l = pd.Series(l)
    c = pd.Series(c)
    hh = h.rolling(n).max()
    ll = l.rolling(n).min()
    rng = (hh - ll).replace(0, np.nan)
    rsv = ((c - ll) / rng * 100.0).fillna(50.0)
    k = rsv.ewm(alpha=1.0 / 3.0, adjust=False).mean()
    d = k.ewm(alpha=1.0 / 3.0, adjust=False).mean()
    j = 3.0 * k - 2.0 * d
    return k.to_numpy(), d.to_numpy(), j.to_numpy()


def macd_hist(c, fast=12, slow=26, sig=9):
    c = pd.Series(c)
    dif = c.ewm(span=fast, adjust=False).mean() - c.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    return (2.0 * (dif - dea)).to_numpy()


def k_band(v: float) -> str:
    if v < 20:
        return "K<20"
    if v < 50:
        return "K20-50"
    if v <= 80:
        return "K50-80"
    return "K>80"


def seg_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    r = np.array([x["r"] for x in rows], dtype=float)
    return {
        "n": int(len(r)),
        "win_rate": round(float((r > 0).mean()) * 100, 2),
        "mean_R": round(float(r.mean()), 5),
        "sum_R": round(float(r.sum()), 1),
        "med_R": round(float(np.median(r)), 5),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--out", default="/tmp/entry_filter.json")
    args = ap.parse_args()

    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts] or []
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    url = settings.db_url
    engine = create_engine(url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()

    p = fusion_params_from_config()
    sl_atr = float(p.sl_atr)

    all_rows: list[dict] = []
    for si, sym in enumerate(symbols):
        df = _read_hourly(session, sym, "bar_60m", None, None)
        if df is None or len(df) < 300:
            print(f"[{si+1}/{len(symbols)}] {sym}: 数据不足({0 if df is None else len(df)})，跳过")
            continue
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        dts = df["dt"].to_numpy()
        n = len(c)
        htf = _htf_direction(c, int(p.ema_k))
        k_arr, d_arr, j_arr = kdj93(h, l, c)
        hist = macd_hist(c)
        hist_abs = np.abs(hist)
        peak20 = pd.Series(hist_abs).rolling(20, min_periods=1).max().to_numpy()
        prev_abs = np.concatenate([[hist_abs[0]], hist_abs[:-1]])

        states = np.zeros(n, dtype=int)
        pos_open: dict | None = None
        rows: list[dict] = []
        prev_state = 0
        for kgen, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
            i = kgen + 2
            st = d["state"]
            states[i] = st
            # 平仓：prev!=0 且 (st==0 或 反向跳变) —— 当根收盘价平（close-only）
            if prev_state != 0 and (st == 0 or st != prev_state) and pos_open:
                e = pos_open
                pos_open = None
                xp = float(c[i])
                r = e["dirn"] * (xp - e["entry_px"]) / (sl_atr * e["entry_atr"])
                rows.append({
                    "symbol": sym, "side": e["side"],
                    "entry_dt": str(pd.Timestamp(dts[e["i"]])),
                    "r": float(r),
                    "k": float(k_arr[e["i"]]), "j": float(j_arr[e["i"]]),
                    "hist": float(hist[e["i"]]),
                    "ratio20": float(hist_abs[e["i"]] / peak20[e["i"]])
                    if peak20[e["i"]] > 0 else 1.0,
                    "expand": bool(hist[e["i"]] * e["dirn"] > 0
                                   and hist_abs[e["i"]] >= prev_abs[e["i"]]),
                })
            # 开仓：prev==0 且 st!=0（反向跳变也视为先平后开的新仓）
            if st != 0 and (prev_state == 0 or st != prev_state):
                e_atr = float(d.get("entry_atr") or 0.0)
                if e_atr > 0:
                    pos_open = {
                        "i": i, "entry_px": float(d["entry_px"]), "entry_atr": e_atr,
                        "dirn": 1.0 if st == 1 else -1.0,
                        "side": "LONG" if st == 1 else "SHORT",
                    }
            prev_state = st
        all_rows.extend(rows)
        print(f"[{si+1}/{len(symbols)}] {sym}: bars={n} trades={len(rows)}")

    session.close()
    engine.dispose()

    # ---------- 分组统计 ----------
    def grp(rows, keyfn):
        out = {}
        for x in rows:
            out.setdefault(keyfn(x), []).append(x)
        return {" | ".join(map(str, kk)): seg_stats(vv)
                for kk, vv in sorted(out.items(), key=lambda t: str(t[0]))}

    is_rows = [x for x in all_rows if pd.Timestamp(x["entry_dt"]) < IS_END]
    oos_rows = [x for x in all_rows if pd.Timestamp(x["entry_dt"]) >= IS_END]

    report = {"total": seg_stats(all_rows), "IS_total": seg_stats(is_rows),
              "OOS_total": seg_stats(oos_rows)}

    for tag, rows in (("ALL", all_rows), ("IS", is_rows), ("OOS", oos_rows)):
        report[f"{tag}__side_x_kband"] = grp(
            rows, lambda x: (x["side"], k_band(x["k"])))
        report[f"{tag}__kdj_adverse"] = grp(
            rows, lambda x: (x["side"], "KDJ逆势超买/超卖" if (
                (x["side"] == "LONG" and x["k"] > 80) or
                (x["side"] == "SHORT" and x["k"] < 20)) else "中性区"))
        report[f"{tag}__macd_adverse"] = grp(
            rows, lambda x: (x["side"], "MACD柱反向" if (
                (x["side"] == "LONG" and x["hist"] < 0) or
                (x["side"] == "SHORT" and x["hist"] > 0)) else "MACD柱同向"))
        report[f"{tag}__macd_deep_squeeze"] = grp(
            rows, lambda x: (x["side"], "深缩<20%峰" if x["ratio20"] < 0.2 else "未深缩"))
        report[f"{tag}__macd_expand"] = grp(
            rows, lambda x: (x["side"], "顺向扩张" if x["expand"] else "其他"))
        report[f"{tag}__combo_F1F2"] = grp(
            rows, lambda x: (x["side"], "F1或F2命中" if (
                ((x["side"] == "LONG" and x["k"] > 80) or
                 (x["side"] == "SHORT" and x["k"] < 20)) or
                ((x["side"] == "LONG" and x["hist"] < 0) or
                 (x["side"] == "SHORT" and x["hist"] > 0))) else "全绿"))
        report[f"{tag}__kbxF2"] = grp(
            rows, lambda x: (k_band(x["k"]), "柱反向" if (
                (x["side"] == "LONG" and x["hist"] < 0) or
                (x["side"] == "SHORT" and x["hist"] > 0) or False) else "柱同向"))

    Path(args.out).write_text(
        json.dumps({"summary": report, "rows_count": len(all_rows)},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"\n总笔数 {len(all_rows)}（IS {len(is_rows)} / OOS {len(oos_rows)}）")
    print(json.dumps({k: report[k] for k in
                      ("total", "IS_total", "OOS_total",
                       "OOS__side_x_kband", "OOS__kdj_adverse",
                       "OOS__macd_adverse", "OOS__macd_deep_squeeze",
                       "OOS__combo_F1F2")},
                     ensure_ascii=False, indent=1))
    print(f"结果已写 {args.out}")


if __name__ == "__main__":
    main()
