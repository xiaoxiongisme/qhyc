#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二期 P1：现成 enabled B 类因子 WF-OOS 评估（研究分支 cb/v2-oos-investigation，未碰 main/cb/dev）。

评估 §12.6 里 7 个 enabled B 类因子中除 basis_rate_z（已在 §14 P0 评估、退役）外的 6 个：
  cross_rank_z / member_ls_z / member_net_z / member_trend_z /
  vol_concentration_z / warehouse_receipt_z
inventory_z / structure_slope_z 因覆盖不足被平台禁用 -> 排除（纪律6）。

★ 防前视（沿用 §14 P0 沉淀，纪律1）：探针已证 l2_adj.factor_value.available_at 对全部 B 类
  因子恒 == trade_date（lag 0），未编码 akshare 现货价/仓单/会员持仓的盘后发布延迟。
  故禁用其对齐，改用显式 shift(lag) 滞后，并跑 lag∈{1,2,3} 稳健性：
  若边只在 lag=1 有、lag>=2 塌 -> 抢跑发布的前视假象，不是真实边。

方向（外部经济先验，非事后挑选）：用平台 factor_registry.default_weight 的**符号**：
  weight>0（高 z 视为看多）-> top-q 做多、bottom-q 做空；
  weight<0（高 z 视为看空，如仓单压力/成交集中度）-> top-q 做空、bottom-q 做多。
  这样「方向」是平台既定的，不构成用回测表现挑方向的选择偏差。
  --both-directions 可额外跑反方向诊断（若权重方向显著为负，再查是否反了）。

口径同 §9 底座：WF-OOS(train720/test60/step60/warmup150)、真实成本、次日 open 成交、
R=净点数/(3×日线ATR)。value_kind=z 为主（cross_rank_z 仅 z 无 raw）。
symbol 用主连 50，因子缺失品种 target=0（交集 44–48）。

用法：
  python signal_factor_eval.py --n-perm 0                        # 只跑 18 格（z 口径）
  python signal_factor_eval.py --both-directions --n-perm 0     # 含反方向诊断（36 格）
  python signal_factor_eval.py --only-perm --perm-cells member_net_z:1:z --n-perm 100
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, seg_stats, placebo_agg, SL_MULT)

FACTORS = ["cross_rank_z", "member_ls_z", "member_net_z", "member_trend_z",
           "vol_concentration_z", "warehouse_receipt_z"]
# 平台 default_weight 符号（外部先验方向）
SIGN = {
    "cross_rank_z": 1, "member_ls_z": 1, "member_net_z": 1, "member_trend_z": 1,
    "vol_concentration_z": -1, "warehouse_receipt_z": -1,
}
WEIGHT = {
    "cross_rank_z": 0.04, "member_ls_z": 0.06, "member_net_z": 0.08,
    "member_trend_z": 0.05, "vol_concentration_z": -0.05, "warehouse_receipt_z": -0.06,
}
LAGS = (1, 2, 3)
Q = 0.2
YEARS_GATE = ("2024", "2025", "2026")
YEARS = ("2022", "2023", "2024", "2025", "2026")


def load_factor_matrices(factors, start, end):
    """一次取多因子 z/raw，pivot 成 {fid: {'z':df,'raw':df}}，index=DatetimeIndex。"""
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4)
    session = sessionmaker(bind=engine)()
    rows = session.execute(text(
        "SELECT factor_id, trade_date, symbol, z_value, raw_value "
        "FROM l2_adj.factor_value "
        "WHERE factor_id = ANY(:fs) AND trade_date >= :s AND trade_date <= :e"
    ), {"fs": list(factors), "s": start - timedelta(days=450), "e": end}).all()
    session.close(); engine.dispose()
    df = pd.DataFrame(rows, columns=["factor_id", "date", "symbol", "z", "raw"])
    df["z"] = pd.to_numeric(df["z"], errors="coerce")
    df["raw"] = pd.to_numeric(df["raw"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    mats = {}
    for fid in factors:
        sub = df[df["factor_id"] == fid]
        mats[fid] = {
            "z": sub.pivot(index="date", columns="symbol", values="z"),
            "raw": sub.pivot(index="date", columns="symbol", values="raw"),
        }
    return mats


def build_targets(closes, mat, q, lag, sign, min_valid=10, ffill_limit=5):
    """横截面分位 targets。★ shift(lag) 显式滞后防前视（available_at 不可信）。"""
    ci = pd.to_datetime(closes.index)
    aligned = mat.reindex(ci).ffill(limit=ffill_limit)
    aligned.index = closes.index
    lagged = aligned.shift(lag)
    pct = lagged.rank(axis=1, pct=True)
    valid = lagged.notna().sum(axis=1)
    tg = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    nn = lagged.notna()
    hi = nn & (pct >= (1 - q))
    lo = nn & (pct <= q)
    tg[hi] = float(sign) * 1.0
    tg[lo] = -float(sign) * 1.0
    tg[valid < min_valid] = 0.0
    return tg.reindex(index=closes.index, columns=closes.columns).fillna(0.0)


def eval_grid(targets, opens, closes, atr, wfp, start, end, rb, contract_map, coef_cache):
    tr = simulate_windows(targets, opens, closes, atr, wfp, start, end,
                          rebalance_n=rb, sl_mult=SL_MULT,
                          contract_map=contract_map, coef_cache=coef_cache)
    o = seg_stats(tr)
    by_year = defaultdict(list)
    for r in tr:
        by_year[_dt(r["entry_dt"]).year].append(r)
    ys = {str(y): seg_stats(v) for y, v in sorted(by_year.items())}
    return o, ys


def gates(o, ys):
    net = o.get("mean_net_R") or 0
    pf = o.get("profit_factor") or 0
    g1 = net > 0
    g2 = isinstance(pf, (int, float)) and pf >= 1.1
    g3 = all((ys.get(y, {}).get("mean_net_R") or 0) > 0 for y in YEARS_GATE)
    return {"netR_pos": g1, "PF_ge_1_1": g2, "year24_26_all_pos": g3,
            "pass_123": g1 and g2 and g3}


def parse_cell(cell):
    parts = cell.split(":")
    fid, lag, kind = parts[0], int(parts[1]), parts[2]
    mode = parts[3] if len(parts) > 3 else "weight"   # weight / anti
    return fid, lag, kind, mode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--q", type=float, default=Q)
    ap.add_argument("--rebalance-n", type=int, default=5)
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--both-directions", action="store_true",
                    help="额外跑权重反方向诊断（不作准入依据）")
    ap.add_argument("--n-perm", type=int, default=0)
    ap.add_argument("--perm-cells", default="",
                    help="逗号分隔，格式 factor_id:lag:value_kind，如 member_net_z:1:z")
    ap.add_argument("--only-perm", action="store_true")
    ap.add_argument("--out", default="/tmp/aeq/factor_eval.json")
    args = ap.parse_args()

    lags = tuple(int(x) for x in args.lags.split(","))
    perm_set = {c.strip() for c in args.perm_cells.split(",") if c.strip()}

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    data_start = start - timedelta(days=400)
    print(f"[init] symbols={len(symbols)} start={start} end={end} lags={lags} "
          f"both_dir={args.both_directions} n_perm={args.n_perm} only_perm={args.only_perm}",
          flush=True)
    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    session.close(); engine.dispose()
    print(f"[load] n_dates={len(opens.index)}", flush=True)

    mats = load_factor_matrices(FACTORS, start, end)
    for fid in FACTORS:
        m = mats[fid]["z"]
        print(f"  [factor] {fid:24s} z_matrix={m.shape} range=[{m.index.min().date()},{m.index.max().date()}]",
              flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    # 方向列表：默认仅权重符号；--both-directions 追加反方向诊断
    dir_modes = [("weight", lambda f: SIGN[f])]
    if args.both_directions:
        dir_modes.append(("anti", lambda f: -SIGN[f]))

    results = []
    if not args.only_perm:
        for fid in FACTORS:
            for mode, sign_fn in dir_modes:
                sign = sign_fn(fid)
                for lag in lags:
                    mat = mats[fid]["z"]
                    if mat.empty or mat.notna().sum().sum() == 0:
                        continue
                    tgt = build_targets(closes, mat, args.q, lag, sign)
                    o, ys = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                                      args.rebalance_n, contract_map, coef_cache)
                    g = gates(o, ys)
                    key = f"{fid}/{mode}/lag{lag}/z"
                    yr = " ".join(f"{y}:{ys.get(y, {}).get('mean_net_R')}" for y in YEARS)
                    results.append({"key": key, "factor_id": fid, "mode": mode,
                                    "sign": sign, "weight": WEIGHT[fid], "lag": lag,
                                    "value_kind": "z", "overall": o, "by_year": ys, "gates": g})
                    print(f"  {key:38s} n={o.get('n'):4d} 净R={o.get('mean_net_R')} "
                          f"PF={o.get('profit_factor')} 过123={g['pass_123']}  年[{yr}]",
                          flush=True)

    # ---- 安慰剂（指定格）----
    placebo = {}
    if args.n_perm > 0:
        for cell in sorted(perm_set):
            fid, lag, kind, mode = parse_cell(cell)
            sign = SIGN[fid] if mode == "weight" else -SIGN[fid]
            mat = mats[fid][kind]
            tgt = build_targets(closes, mat, args.q, lag, sign)
            o, ys = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                              args.rebalance_n, contract_map, coef_cache)
            pct = placebo_agg(tgt, opens, closes, atr, wfp, start, end,
                              args.rebalance_n, SL_MULT, o["sum_net_R"], n_perm=args.n_perm,
                              contract_map=contract_map, coef_cache=coef_cache)
            placebo[cell] = {"overall": o, "by_year": ys, "mode": mode, "sign": sign,
                             "rand_ge_actual_pct": pct, "n_perm": args.n_perm}
            print(f"[placebo] {cell}: n={o.get('n')} 净R={o.get('mean_net_R')} "
                  f"PF={o.get('profit_factor')} rand_ge_actual%={pct}", flush=True)

    # ---- 汇总 ----
    summary = {}
    if results:
        wgrp = [r for r in results if r["mode"] == "weight"]
        summary["weight_direction"] = {
            "n_grids": len(wgrp),
            "netR_pos_grids": sum(1 for r in wgrp if (r["overall"].get("mean_net_R") or 0) > 0),
            "pf_ge_1_1_grids": sum(1 for r in wgrp
                                   if isinstance(r["overall"].get("profit_factor"), (int, float))
                                   and r["overall"]["profit_factor"] >= 1.1),
            "year24_26_all_pos_grids": sum(1 for r in wgrp if r["gates"]["year24_26_all_pos"]),
            "pass_123_grids": sum(1 for r in wgrp if r["gates"]["pass_123"]),
        }
        # 逐因子 lag 稳健性（权重方向）
        per_factor = {}
        for fid in FACTORS:
            rows = [r for r in wgrp if r["factor_id"] == fid]
            per_factor[fid] = {
                "weight": WEIGHT[fid],
                "lags": [{"lag": r["lag"], "mean_net_R": r["overall"].get("mean_net_R"),
                          "PF": r["overall"].get("profit_factor"), "n": r["overall"].get("n")}
                         for r in rows],
                "pass_123_any_lag": any(r["gates"]["pass_123"] for r in rows),
            }
        summary["per_factor_weight_direction"] = per_factor
        if args.both_directions:
            agrp = [r for r in results if r["mode"] == "anti"]
            summary["anti_direction_diagnostic"] = {
                "n_grids": len(agrp),
                "pass_123_grids": sum(1 for r in agrp if r["gates"]["pass_123"]),
                "note": "反方向仅作方向核验诊断，不作准入依据",
            }

    report = {
        "meta": {"start": str(start), "end": str(end), "factors": FACTORS,
                 "weights": WEIGHT, "sign_from_weight": True, "lags": list(lags),
                 "q": args.q, "rebalance_n": args.rebalance_n, "value_kind": "z",
                 "both_directions": args.both_directions,
                 "available_at_note": "探针证实 available_at==trade_date(lag0)，禁用其对齐，"
                                      "改用显式 shift(lag) 滞后防前视（沿用 §14 P0）",
                 "direction_note": "方向取平台 default_weight 符号，外部先验、非事后挑选",
                 "n_windows": len(list(_windows(start, end, wfp)))},
        "grids": results, "summary": summary, "placebo": placebo,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print("\n=== SUMMARY ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    main()
