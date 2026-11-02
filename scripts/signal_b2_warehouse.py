#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四期 P0：B2 仓单「按品种聚合重算」WF-OOS 评估（研究分支，未碰 main/cb/dev）。

回应 §12.4 / §16.7 待办「B2 仓单按品种聚合原始仓单重算 z，先解决方向与发布时点问题」。
与 §15 的区别：§15 用的是平台 l2_adj.factor_value.warehouse_receipt_z（现成 z-score，
available_at 恒 lag0）；本脚本从 l0_raw.warehouse_receipt 多仓库明细**重新聚合**构造。

★ 数据质量（探针 probe_p4_warehouse.json，纪律6）：
  * 897,063 行多仓库明细（每 report_date×symbol×warehouse 一行，仓库数 1–46）；
    receipt_qty 空 19.6% + 零 44.3% = 64% -> 必须先聚合 sum(receipt_qty)。
  * symbol 为 888 口径 74 品种，与主连交集 49；PTA888 是主连 TA888 的别名 -> 需映射。
  * 覆盖度极不均：仅 15 品种 >=800 天，21 品种 <400 天，18 品种 2026-10 才纳入
    -> 横截面用 min_valid 门槛 + ffill(limit=5)，短样本品种自动被 pct_change 排除。
  * change_qty 与自算 sum(receipt) 日差分吻合 99.7-100% -> 用聚合序列自算变化率，更可控。
  * created_at - report_date = 0 ~ 2277 天（均值 448-1050），典型批量回填
    -> 不可作发布时点；沿用 §14/§15 显式 shift(lag) + lag∈{1,2,3} 稳健性。

★ 预注册（不事后挑方向/挑构造）：
  主判据构造 chg5 = agg.pct_change(5)（周频仓单变化率），对应 rebalance_n=5；
  方向 sign=-1：仓单增加 = 可交割供给宽松 = 偏空（高值做空）。经济先验，非回测挑选。
  稳健性构造 chg20（月频）、level_z（60 日时序 z）同方向；反方向仅诊断、不作准入。
  lag 稳健性是**前视证伪工具**：若边只在 lag=1 有、lag>=2 塌 = 抢跑发布假象。

口径同 §9 底座：WF-OOS(train720/test60/step60/warmup150)、真实成本、次日 open 成交、
R=净点数/(3×日线ATR)、§6 五门槛。安慰剂置换沿用 placebo_agg。

用法：
  python signal_b2_warehouse.py --n-perm 0                     # 先验方向 9 格
  python signal_b2_warehouse.py --both-directions --n-perm 0  # + 反方向诊断 9 格
  python signal_b2_warehouse.py --only-perm --perm-cells chg5:1:prior --n-perm 100
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
from signal_factor_eval import build_targets, gates, eval_grid, parse_cell

# 与主连口径的别名映射（探针确认：主连 TA888，仓单表 PTA888）
ALIAS = {"PTA888": "TA888"}
KINDS = ("chg5", "chg20", "level_z")
LAGS = (1, 2, 3)
Q = 0.2
REBAL = {"chg5": 5, "chg20": 21, "level_z": 5}   # 变化率窗口与调仓频率匹配
YEARS = ("2022", "2023", "2024", "2025", "2026")


def load_warehouse(session, symbols, start, end, closes_index):
    """按品种×日聚合 sum(receipt_qty)，映射别名，reindex 到行情交易日并 ffill(limit=5)。"""
    rows = session.execute(text("""
        SELECT report_date, symbol, sum(receipt_qty) AS tot
        FROM l0_raw.warehouse_receipt
        WHERE report_date >= :s AND report_date <= :e
        GROUP BY report_date, symbol
    """), {"s": start, "e": end}).all()
    df = pd.DataFrame(rows, columns=["date", "symbol", "tot"])
    df["date"] = pd.to_datetime(df["date"])
    df["tot"] = pd.to_numeric(df["tot"], errors="coerce")
    df["symbol"] = df["symbol"].replace(ALIAS)
    df = df[df["symbol"].isin(symbols)]
    mat = df.pivot_table(index="date", columns="symbol", values="tot", aggfunc="sum")
    ci = pd.to_datetime(closes_index)
    mat = mat.reindex(ci).ffill(limit=5)
    mat.index = closes_index
    return mat.reindex(columns=symbols)


def build_matrix(agg, kind):
    """由聚合仓单量构造信号矩阵（横截面可比）。fill_method=None 避免隐式 pad 前视。"""
    if kind == "chg5":
        out = agg.pct_change(5, fill_method=None)
    elif kind == "chg20":
        out = agg.pct_change(20, fill_method=None)
    elif kind == "level_z":
        m = agg.rolling(60, min_periods=30).mean()
        s = agg.rolling(60, min_periods=30).std()
        out = (agg - m) / s.replace(0, np.nan)
    else:
        raise ValueError(kind)
    return out.replace([np.inf, -np.inf], np.nan)


def cross_sectional_ic(mat, closes, horizon=5, lag=1, min_valid=10, is_cut="2023-12-31"):
    """横截面 IC 核验先验方向：每日 spearman(signal_t, fwd_ret_h)，分 IS/OOS/逐年。
    注意 IC 用 lag 后的信号（与回测一致），fwd_ret 用 close-to-close，仅作方向定性。"""
    lagged = mat.shift(lag)
    fwd = closes.shift(-horizon) / closes - 1.0
    fwd = fwd.replace([np.inf, -np.inf], np.nan)
    dates = mat.index
    ic_series, ic_year = [], defaultdict(list)
    is_cut_ts = pd.Timestamp(is_cut)
    for d in dates:
        s = lagged.loc[d]
        f = fwd.loc[d] if d in fwd.index else None
        if f is None:
            continue
        common = s.dropna().index.intersection(f.dropna().index)
        if len(common) < min_valid:
            continue
        a = s.loc[common].rank()
        b = f.loc[common].rank()
        rho = float(np.corrcoef(a.to_numpy(), b.to_numpy())[0, 1])
        if rho != rho:
            continue
        ic_series.append((d, rho))
        ic_year[str(pd.Timestamp(d).year)].append(rho)
    def stat(v):
        v = np.asarray(v, dtype=float)
        if len(v) < 3:
            return {"n": len(v), "ic": None, "t": None}
        mu, sd = float(v.mean()), float(v.std(ddof=1))
        t = mu / (sd / np.sqrt(len(v))) if sd > 0 else None
        return {"n": len(v), "ic": round(mu, 5), "t": round(t, 3) if t is not None else None}
    full = stat([x for _, x in ic_series])
    isv = stat([x for d, x in ic_series if pd.Timestamp(d) <= is_cut_ts])
    oos = stat([x for d, x in ic_series if pd.Timestamp(d) > is_cut_ts])
    return {"full": full, "is": isv, "oos": oos,
            "by_year": {y: stat(v) for y, v in sorted(ic_year.items())},
            "horizon": horizon, "lag": lag, "min_valid": min_valid}


def parse_b2_cell(cell):
    """kind:lag:mode ，mode=prior(sign=-1) / anti(sign=+1)"""
    kind, lag, mode = cell.split(":")
    return kind, int(lag), mode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--q", type=float, default=Q)
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--both-directions", action="store_true",
                    help="额外跑反方向诊断（不作准入依据）")
    ap.add_argument("--n-perm", type=int, default=0)
    ap.add_argument("--perm-cells", default="",
                    help="逗号分隔，格式 kind:lag:mode，如 chg5:1:prior")
    ap.add_argument("--only-perm", action="store_true")
    ap.add_argument("--out", default="/tmp/aeq/b2_warehouse.json")
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
          f"both_dir={args.both_directions} n_perm={args.n_perm}", flush=True)
    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache: dict = {}
    agg = load_warehouse(session, symbols, data_start, end, opens.index)
    session.close(); engine.dispose()
    print(f"[load] n_dates={len(opens.index)} agg={agg.shape} "
          f"agg_notna={int(agg.notna().sum().sum())} "
          f"agg_cols_with_data={int((agg.notna().sum() > 0).sum())}", flush=True)
    for kind in KINDS:
        m = build_matrix(agg, kind)
        print(f"  [matrix] {kind:8s} shape={m.shape} notna={int(m.notna().sum().sum())} "
              f"range=[{np.nanmin(m.to_numpy()):.4f},{np.nanmax(m.to_numpy()):.4f}]", flush=True)

    # ---- 先验方向 IC 核验（不依赖仓位构造，§17 教训）----
    ic = {}
    for kind in KINDS:
        m = build_matrix(agg, kind)
        h = 5 if kind in ("chg5", "level_z") else 20
        ic[kind] = cross_sectional_ic(m, closes, horizon=h, lag=1)
        print(f"  [IC] {kind:8s} full={ic[kind]['full']} IS={ic[kind]['is']} OOS={ic[kind]['oos']}",
              flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    modes = [("prior", -1)]
    if args.both_directions:
        modes.append(("anti", +1))

    results = []
    if not args.only_perm:
        for kind in KINDS:
            mat = build_matrix(agg, kind)
            rb = REBAL[kind]
            for mode, sign in modes:
                for lag in lags:
                    tgt = build_targets(closes, mat, args.q, lag, sign)
                    o, ys = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                                      rb, contract_map, coef_cache)
                    g = gates(o, ys)
                    key = f"{kind}/{mode}/lag{lag}"
                    yr = " ".join(f"{y}:{ys.get(y, {}).get('mean_net_R')}" for y in YEARS)
                    results.append({"key": key, "kind": kind, "mode": mode, "sign": sign,
                                    "lag": lag, "rebalance_n": rb,
                                    "overall": o, "by_year": ys, "gates": g})
                    print(f"  {key:26s} n={o.get('n'):4d} 净R={o.get('mean_net_R')} "
                          f"PF={o.get('profit_factor')} 过123={g['pass_123']}  年[{yr}]",
                          flush=True)

    # ---- 安慰剂 ----
    placebo = {}
    if args.n_perm > 0:
        for cell in sorted(perm_set):
            kind, lag, mode = parse_b2_cell(cell)
            sign = -1 if mode == "prior" else 1
            mat = build_matrix(agg, kind)
            rb = REBAL[kind]
            tgt = build_targets(closes, mat, args.q, lag, sign)
            o, ys = eval_grid(tgt, opens, closes, atr, wfp, start, end,
                              rb, contract_map, coef_cache)
            pct = placebo_agg(tgt, opens, closes, atr, wfp, start, end,
                              rb, SL_MULT, o["sum_net_R"], n_perm=args.n_perm,
                              contract_map=contract_map, coef_cache=coef_cache)
            placebo[cell] = {"overall": o, "by_year": ys, "mode": mode, "sign": sign,
                             "rebalance_n": rb, "rand_ge_actual_pct": pct,
                             "n_perm": args.n_perm}
            print(f"[placebo] {cell}: n={o.get('n')} 净R={o.get('mean_net_R')} "
                  f"PF={o.get('profit_factor')} rand_ge_actual%={pct}", flush=True)

    # ---- 汇总 ----
    summary = {}
    if results:
        pgrp = [r for r in results if r["mode"] == "prior"]
        summary["prior_direction"] = {
            "n_grids": len(pgrp),
            "netR_pos_grids": sum(1 for r in pgrp if (r["overall"].get("mean_net_R") or 0) > 0),
            "pf_ge_1_1_grids": sum(1 for r in pgrp
                                    if isinstance(r["overall"].get("profit_factor"), (int, float))
                                    and r["overall"]["profit_factor"] >= 1.1),
            "year24_26_all_pos_grids": sum(1 for r in pgrp if r["gates"]["year24_26_all_pos"]),
            "pass_123_grids": sum(1 for r in pgrp if r["gates"]["pass_123"]),
        }
        # 逐构造 lag 稳健性（先验方向）：lag1 有边但 lag>=2 塌 => 前视假象
        per_kind = {}
        for kind in KINDS:
            rows = [r for r in pgrp if r["kind"] == kind]
            per_kind[kind] = {
                "lags": [{"lag": r["lag"], "mean_net_R": r["overall"].get("mean_net_R"),
                          "PF": r["overall"].get("profit_factor"), "n": r["overall"].get("n")}
                         for r in rows],
                "pass_123_any_lag": any(r["gates"]["pass_123"] for r in rows),
                "lag_robust": all((r["overall"].get("mean_net_R") or 0) > 0 for r in rows),
            }
        summary["per_kind_prior"] = per_kind
        if args.both_directions:
            agrp = [r for r in results if r["mode"] == "anti"]
            summary["anti_direction_diagnostic"] = {
                "n_grids": len(agrp),
                "pass_123_grids": sum(1 for r in agrp if r["gates"]["pass_123"]),
                "note": "反方向仅作方向核验诊断，不作准入依据",
            }

    report = {
        "meta": {"start": str(start), "end": str(end), "kinds": list(KINDS),
                 "lags": list(lags), "q": args.q, "rebalance_by_kind": REBAL,
                 "alias_map": ALIAS,
                 "direction_note": "先验固定 sign=-1（仓单增加=供给宽松=偏空）；反方向仅诊断",
                 "preshift_note": "created_at 批量回填不可用；显式 shift(lag)+lag∈{1,2,3} 稳健性",
                 "data_quality_note": "多仓库明细 sum(receipt_qty) 聚合；空19.6%+零44.3%；"
                                      "与主连交集49；仅15品种>=800天",
                 "n_windows": len(list(_windows(start, end, wfp)))},
        "ic": ic, "grids": results, "summary": summary, "placebo": placebo,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print("\n=== SUMMARY ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    main()
