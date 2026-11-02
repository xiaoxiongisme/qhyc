#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""五期 P1：B3 真实资金占用折算 / 复利资金曲线（§19.7 待办①）。

§19 口径是「固定名义本金、单腿归一收益率、简单加总、无复利、无持仓资金成本」，
总收益 1.89933 偏高估、暴露仅 5.74%。本节在同一批 195 笔底座成交上**事后重算**
（不改 simulate、与 §9/§10/§11/§19 逐位可比），补上资金占用维度：

  数据：l3_ref.dim_variety（26/26 品种覆盖，multiplier / margin_rate_long / margin_rate_short）
        对照档 l3_ref.cfg_symbol_margin.broker_margin_ratio（期货公司保证金，更保守）
        参考 public.futures_rule 带 trade_date 历史率（仅报告覆盖度，不作主口径）
  单腿 → 对聚合：B3 每对同日开两腿（一多一空），按 (entry_dt, pair) 归并，统计配对完整性。
  每腿保证金 = entry_px × multiplier × margin_rate(side)；净金额 = (pnl − cost×mult) × multiplier。

并排口径（均基于同一批成交）：
  A §19 基线校验   Σ (pnl−cost)/entry_px（单腿）—— 必须 = 1.89933 / n=195
  B 保证金回报率    Σ (对净金额 / 对保证金)，简单加总
  C 账户线性曲线    E0 + Σ 净金额，E0 = 峰值单日保证金占用（保守分母，资金效率可解释）
  D 固定分数复利    NAV *= (1 + f·对保证金回报率)，按 entry 排序，f∈{0.25,0.5,1.0} 敏感性
  E 分年 / 暴露 / 利用率

诚实声明（写入 doc 与 JSON caveat）：
  1 静态保证金率（dim updated_at 2026-10 / cfg 汇总表20260324），非逐日历史对齐 → 近似；
    futures_rule 有历史维度但覆盖度待核，仅参考不作主口径。
  2 固定 1 手/腿规模，未做「按权益动态缩放手数 + 整数约束 + 逐日盯市」的真实动态复利；
    固定分数复利为理论敏感性，隐含无并行占用冲突，属上界。
  3 未计资金成本/利息、未计组合保证金优惠、未计滑点随规模变化；两腿保证金全额计。
  4 并行对数上限与真实容量见 §11；本节只折算已成交样本，不外推。

用法：python signal_b3_capital.py --pair-set ext --window 60 --entry-z 2.0 --end 2026-10-30 --out ...
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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import _windows, _dt, WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, seg_stats, SL_MULT)
from signal_b3_pairs import PAIRS as BASE_PAIRS, compute_targets
from signal_b3_grid import EXT_PAIRS
from signal_b3_capacity import r_stats, pair_of


def asdate(t):
    """统一成 datetime.date（datetime/Timestamp 都是 date 子类，不能只靠 isinstance(date) 判断）。"""
    d = _dt(t)
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


B3_VARIETIES = ("RB", "HC", "M", "RM", "Y", "P", "TA", "MA", "FG", "SA",
                "J", "JM", "PP", "L", "AU", "AG", "FU", "SC", "SF", "SM",
                "C", "CS", "CU", "ZN", "NI", "SS")

# 三种规模假设（决定金额口径的符号，必须并排，不可只报一种）：
#   notional  每腿等名义本金 → size = NOTIONAL/(entry_px×mult)，与 §19 归一收益率一致
#   fixed_lot 每腿固定 1 手   → size = 1，大乘数品种（SC/AU=1000, J=100）主导
#   risk      每笔等风险金额 → size = RISK_CAPITAL/(risk_pts×mult)，对应 R 口径
NOTIONAL_PER_LEG = 1_000_000.0
RISK_CAPITAL = 100_000.0


def load_margin(session):
    dim, brk, fr_hist = {}, {}, defaultdict(list)
    rows = session.execute(text("""
        SELECT variety_code, multiplier, margin_rate_long, margin_rate_short
        FROM l3_ref.dim_variety
    """)).all()
    for v, mult, rl, rs in rows:
        dim[str(v).upper()] = (float(mult or 0), float(rl or 0), float(rs or 0))
    rows = session.execute(text("""
        SELECT variety_code, broker_margin_ratio, exchange_margin_ratio
        FROM l3_ref.cfg_symbol_margin
    """)).all()
    for v, br, ex in rows:
        brk[str(v).upper()] = (float(br) if br is not None else None,
                               float(ex) if ex is not None else None)
    # 历史率：public.futures_rule 带 trade_date，可做 2023+ 逐日对齐对照（2021-22 缺失须回退）
    fr = session.execute(text("""
        SELECT min(trade_date)::text, max(trade_date)::text,
               count(DISTINCT trade_date), count(DISTINCT code)
        FROM public.futures_rule
    """)).one()
    fr_rows = session.execute(text("""
        SELECT code, trade_date, margin_ratio FROM public.futures_rule
        WHERE margin_ratio IS NOT NULL ORDER BY code, trade_date
    """)).all()
    for code, td, mr in fr_rows:
        fr_hist[str(code).upper()].append((td, float(mr) / 100.0))
    fr_info = {"min_trade_date": fr[0], "max_trade_date": fr[1],
               "n_dates": int(fr[2] or 0), "n_codes": int(fr[3] or 0),
               "n_b3_varieties_covered": len(set(fr_hist) & set(B3_VARIETIES)),
               "note": "margin_ratio 为百分数，已 /100 转比例；2021-2022 无记录，回退 dim 静态率"}
    return dim, brk, fr_info, fr_hist


def resolve_rate(var, side, entry, rate_mode, dim, brk, fr_hist):
    """返回 (multiplier, rate, fell_back)。futures_rule 档按 entry 取 <= 最近历史率。"""
    mult, rl, rs = dim[var]
    static = rl if side == "LONG" else rs
    if rate_mode == "broker":
        b = brk.get(var, (None, None))[0]
        return mult, (b if b else static), False
    if rate_mode == "futures_rule":
        cand = [r for td, r in fr_hist.get(var, []) if asdate(td) <= entry]
        if cand:
            return mult, cand[-1], False
        return mult, static, True
    return mult, static, False


def build_legs(trades, dim, brk, fr_hist, rate_mode="dim", cost_mult=1.0,
               size_mode="notional"):
    legs, missing, n_fallback = [], [], 0
    for t in trades:
        sym = t["symbol"]
        var = sym[:-3] if sym.endswith("888") else sym
        if var not in dim:
            missing.append(sym)
            continue
        entry = asdate(t["entry_dt"])
        mult, rate, fell = resolve_rate(var, t["side"], entry, rate_mode,
                                        dim, brk, fr_hist)
        n_fallback += int(fell)
        if mult <= 0 or rate <= 0:
            missing.append(sym)
            continue
        if size_mode == "notional":
            size = NOTIONAL_PER_LEG / (t["entry_px"] * mult)
        elif size_mode == "risk":
            rp = float(t["risk"])
            size = RISK_CAPITAL / (rp * mult) if rp > 0 else 0.0
        else:  # fixed_lot
            size = 1.0
        if size <= 0:
            missing.append(sym)
            continue
        margin = size * t["entry_px"] * mult * rate
        net_amt = size * (t["pnl"] - t["cost_pts"] * cost_mult) * mult
        legs.append({
            "symbol": sym, "var": var, "side": t["side"],
            "entry": entry, "exit": asdate(t["exit_dt"]),
            "entry_px": t["entry_px"], "mult": mult, "rate": rate,
            "size": size, "fell_back": fell,
            "margin": margin, "net_amt": net_amt,
            "net_ret_px": (t["pnl"] - t["cost_pts"] * cost_mult) / t["entry_px"],
        })
    return legs, missing, n_fallback


def pair_aggregate(legs, pairs):
    groups = defaultdict(list)
    for lg in legs:
        groups[(lg["entry"], pair_of(lg["symbol"], pairs))].append(lg)
    out, incomplete = [], []
    for (entry, pname), gl in sorted(groups.items()):
        marg = sum(x["margin"] for x in gl)
        net = sum(x["net_amt"] for x in gl)
        exits = sorted({x["exit"] for x in gl})
        item = {"pair": pname, "entry": entry, "exit": max(exits),
                "n_legs": len(gl), "sides": sorted({x["side"] for x in gl}),
                "margin": marg, "net_amt": net,
                "ret_on_margin": net / marg if marg > 0 else None,
                "exit_consistent": len(exits) == 1}
        out.append(item)
        if len(gl) != 2 or len(item["sides"]) != 2 or not item["exit_consistent"]:
            incomplete.append(item)
    return out, incomplete, len(groups)


def daily_curve(legs, calendar):
    """按日资金占用 + 已实现收益（固定 1 手/腿，线性累积，最可比基线）。"""
    cal = [asdate(d) for d in calendar]
    by_exit = defaultdict(float)
    for lg in legs:
        by_exit[lg["exit"]] += lg["net_amt"]
    occ, realized = [], []
    for d in cal:
        o = sum(lg["margin"] for lg in legs if lg["entry"] <= d < lg["exit"])
        occ.append(o)
        realized.append(by_exit.get(d, 0.0))
    occ = np.array(occ, dtype=float)
    realized = np.array(realized, dtype=float)
    peak_occ = float(occ.max()) if len(occ) else 0.0
    mean_occ = float(occ.mean()) if len(occ) else 0.0
    active = int((occ > 0).sum())
    cum = np.cumsum(realized)
    return {"calendar": [str(d) for d in cal], "occ": occ, "realized": realized,
            "cum": cum, "peak_occ": peak_occ, "mean_occ": mean_occ,
            "active_days": active, "n_days": len(cal)}


def compound_curve(pairs, fs=(0.25, 0.5, 1.0)):
    """固定分数复利：按 entry 排序，NAV *= (1 + f·对保证金回报率)。
    隐含「每笔独占资金、无并行冲突」——属上界，仅作敏感性。"""
    seq = sorted([p for p in pairs if p["ret_on_margin"] is not None],
                 key=lambda p: p["entry"])
    out = {}
    for f in fs:
        nav, peak, mdd, curve = 1.0, 1.0, 0.0, []
        for p in seq:
            nav *= (1.0 + f * p["ret_on_margin"])
            peak = max(peak, nav)
            mdd = max(mdd, (peak - nav) / peak)
            curve.append(nav)
        out[str(f)] = {"final_nav": round(nav, 5), "total_return": round(nav - 1, 5),
                       "mdd_frac": round(mdd, 5), "n_pairs": len(seq),
                       "curve_tail": [round(x, 5) for x in curve[-5:]]}
    return out


def stats_block(legs, pairs, calendar, e0):
    dc = daily_curve(legs, calendar)
    cum = dc["cum"]
    total_amt = float(cum[-1]) if len(cum) else 0.0
    # 账户净值曲线（E0 起，线性累积）
    nav = e0 + cum
    peak = np.maximum.accumulate(nav)
    mdd_amt = float((peak - nav).max()) if len(nav) else 0.0
    daily_ret = np.diff(np.concatenate([[e0], nav])) / e0 if e0 > 0 else np.zeros(len(nav))
    sd = float(daily_ret.std(ddof=1)) if len(daily_ret) > 1 else 0.0
    sharpe = float(daily_ret.mean() / sd * np.sqrt(252)) if sd > 0 else None
    span = (dc["calendar"][-1], dc["calendar"][0])
    years = (date.fromisoformat(span[0]) - date.fromisoformat(span[1])).days / 365.25
    return {
        "e0_peak_margin_based": round(e0, 2),
        "total_net_amount": round(total_amt, 2),
        "capital_efficiency_on_peak": round(total_amt / e0, 5) if e0 > 0 else None,
        "annualized_capital_efficiency": round(total_amt / e0 / years, 5) if e0 > 0 and years > 0 else None,
        "peak_margin_occ": round(dc["peak_occ"], 2),
        "mean_margin_occ": round(dc["mean_occ"], 2),
        "mean_utilization_pct": round(100 * dc["mean_occ"] / e0, 2) if e0 > 0 else None,
        "exposure_days_pct": round(100 * dc["active_days"] / dc["n_days"], 2),
        "n_calendar_days": dc["n_days"], "n_active_days": dc["active_days"],
        "mdd_amount": round(mdd_amt, 2),
        "mdd_on_e0": round(mdd_amt / e0, 5) if e0 > 0 else None,
        "sharpe_daily": round(sharpe, 3) if sharpe is not None else None,
        "span_years": round(years, 2),
        "nav_head": [round(x, 2) for x in nav[:5]],
        "nav_tail": [round(x, 2) for x in nav[-5:]],
    }


def by_year_block(legs, pairs, calendar):
    out = {}
    years = sorted({lg["entry"].year for lg in legs})
    for y in years:
        lg = [x for x in legs if x["entry"].year == y]
        pr = [x for x in pairs if x["entry"].year == y]
        cal = [d for d in calendar if asdate(d).year == y]
        dc = daily_curve(lg, cal)
        e0 = dc["peak_occ"] or 1.0
        sm = sum_ret = 0.0
        tot_amt = float(dc["cum"][-1]) if len(dc["cum"]) else 0.0
        ret_on_margin = sum(p["ret_on_margin"] for p in pr if p["ret_on_margin"] is not None)
        out[str(y)] = {
            "n_legs": len(lg), "n_pairs": len(pr),
            "net_return_px_simple": round(sum(x["net_ret_px"] for x in lg), 5),
            "sum_ret_on_margin": round(ret_on_margin, 5),
            "total_net_amount": round(tot_amt, 2),
            "peak_margin_occ": round(dc["peak_occ"], 2),
            "capital_efficiency_on_peak": round(tot_amt / e0, 5) if e0 > 0 else None,
            "exposure_days_pct": round(100 * dc["active_days"] / dc["n_days"], 2),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--pair-set", choices=["base", "ext"], default="ext")
    ap.add_argument("--rate-mode", choices=["dim", "broker", "futures_rule"], default="dim")
    ap.add_argument("--size-mode", choices=["notional", "fixed_lot", "risk"], default="notional")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--out", default="/tmp/b3_capital.json")
    args = ap.parse_args()

    pairs = list(BASE_PAIRS) if args.pair_set == "base" else list(BASE_PAIRS) + EXT_PAIRS
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    dim, brk, fr_info, fr_hist = load_margin(session)
    print(f"[init] pair_set={args.pair_set} n_pairs={len(pairs)} rate_mode={args.rate_mode} "
          f"size_mode={args.size_mode} cost_mult={args.cost_mult} "
          f"dim_varieties={len(dim)} cfg_margin={len(brk)} fr_hist_codes={len(fr_hist)}", flush=True)
    print(f"[margin] futures_rule 覆盖: {fr_info}", flush=True)

    opens, closes, highs, lows = load_market(session, symbols, start - timedelta(days=400), end)
    atr = daily_atr(highs, lows, closes)
    contract_map = main_contract_surface(session, symbols, list(opens.index))
    coef_cache = {}
    session.close(); engine.dispose()
    calendar = [d for d in opens.index if start <= asdate(d) <= end]
    print(f"[load] calendar_in_window={len(calendar)}", flush=True)

    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    tgt, _ = compute_targets(closes, pairs=pairs, window=args.window,
                             entry_z=args.entry_z, exit_z=args.exit_z,
                             return_strength=True)
    t = tgt.reindex(index=opens.index, columns=symbols).fillna(0.0)
    trades = simulate_windows(t, opens, closes, atr, wfp, start, end,
                              rebalance_n=1, sl_mult=SL_MULT,
                              contract_map=contract_map, coef_cache=coef_cache)
    base = seg_stats(trades)
    # A §19 基线校验
    simple_sum = sum((x["pnl"] - x["cost_pts"] * args.cost_mult) / x["entry_px"]
                     for x in trades)
    print(f"[baseline] n={base.get('n')} 净R={base.get('mean_net_R')} PF={base.get('profit_factor')} "
          f"§19简单加总={round(simple_sum,5)} (须 n=195/0.19007/1.734/1.89933)", flush=True)

    legs, missing, n_fallback = build_legs(trades, dim, brk, fr_hist,
                                           args.rate_mode, args.cost_mult,
                                           args.size_mode)
    pagg, incomplete, n_groups = pair_aggregate(legs, pairs)
    print(f"[aggregate] legs={len(legs)} missing={len(set(missing))} "
          f"pair_groups={n_groups} incomplete={len(incomplete)} "
          f"rate_fallback_legs={n_fallback}", flush=True)
    # 单腿裸露头寸：非配对成交（未对冲，方向性暴露），生产上应单列/剔除
    naked_keys = {(x["entry"], x["pair"]) for x in incomplete}
    naked_amt = sum(lg["net_amt"] for lg in legs
                    if (lg["entry"], pair_of(lg["symbol"], pairs)) in naked_keys)
    clean_amt = sum(lg["net_amt"] for lg in legs
                    if (lg["entry"], pair_of(lg["symbol"], pairs)) not in naked_keys)
    print(f"[naked] 单腿裸露组={len(incomplete)} 其净金额={round(naked_amt,2)} "
          f"剔除裸露后净金额={round(clean_amt,2)}", flush=True)
    if incomplete:
        print("  [warn] 非标准配对样例:", flush=True)
        for x in incomplete[:5]:
            print("   ", {k: str(v) for k, v in x.items()}, flush=True)

    dc = daily_curve(legs, calendar)
    e0 = dc["peak_occ"] or 1.0
    main_stats = stats_block(legs, pagg, calendar, e0)
    comp = compound_curve(pagg)
    by_year = by_year_block(legs, pagg, calendar)

    print("\n=== B 保证金回报率（简单加总）===", flush=True)
    print(f"  Σ对保证金回报率={round(sum(p['ret_on_margin'] for p in pagg if p['ret_on_margin'] is not None),5)} "
          f"n_pairs={len(pagg)}", flush=True)
    print("\n=== C 账户线性曲线（E0=峰值保证金占用）===", flush=True)
    print(f"  E0={main_stats['e0_peak_margin_based']} 总净金额={main_stats['total_net_amount']} "
          f"资金效率(峰值)={main_stats['capital_efficiency_on_peak']} "
          f"年化={main_stats['annualized_capital_efficiency']} "
          f"平均利用率={main_stats['mean_utilization_pct']}% "
          f"暴露={main_stats['exposure_days_pct']}% MDD/E0={main_stats['mdd_on_e0']} "
          f"Sharpe={main_stats['sharpe_daily']}", flush=True)
    print("\n=== D 固定分数复利（上界敏感性）===", flush=True)
    for f, v in comp.items():
        print(f"  f={f}: 终值NAV={v['final_nav']} 总收益={v['total_return']} MDD={v['mdd_frac']}", flush=True)
    print("\n=== E 分年 ===", flush=True)
    print("  year  n_legs n_pairs 归一收益  保证金回报  净金额    峰值占用  资金效率  暴露%", flush=True)
    for y, v in by_year.items():
        print(f"  {y}  {v['n_legs']:5d} {v['n_pairs']:6d}  "
              f"{str(v['net_return_px_simple']):9s} {str(v['sum_ret_on_margin']):10s} "
              f"{str(v['total_net_amount']):9s} {str(v['peak_margin_occ']):9s} "
              f"{str(v['capital_efficiency_on_peak']):9s} {v['exposure_days_pct']}", flush=True)

    report = {
        "meta": {"start": str(start), "end": str(end), "pair_set": args.pair_set,
                 "window": args.window, "entry_z": args.entry_z, "exit_z": args.exit_z,
                 "rate_mode": args.rate_mode, "cost_mult": args.cost_mult,
                 "size_mode": args.size_mode,
                 "notional_per_leg": NOTIONAL_PER_LEG, "risk_capital": RISK_CAPITAL,
                 "n_windows": len(list(_windows(start, end, wfp))),
                 "margin_source": {"dim": "l3_ref.dim_variety (static, updated 2026-10)",
                                   "broker": "l3_ref.cfg_symbol_margin.broker_margin_ratio (static 20260324)",
                                   "futures_rule": "public.futures_rule 按建仓日 <= 最近历史率（2023+），缺则回退 dim"}[args.rate_mode],
                 "futures_rule_coverage": fr_info,
                 "size_rule": {"notional": "每腿 size=NOTIONAL/(entry_px×multiplier)，与 §19 等名义本金一致",
                               "fixed_lot": "每腿固定 1 手，大乘数品种主导金额盈亏",
                               "risk": "每笔 size=RISK_CAPITAL/(risk_pts×multiplier)，对应 R 口径"}[args.size_mode],
                 "amount_rule": "net_amt=size×(pnl−cost×cost_mult)×multiplier；margin=size×entry_px×multiplier×rate(side)",
                 "caveat": [
                     "★ 金额口径符号由规模假设决定：等名义本金(§19口径)为正、固定1手因大乘数品种主导为负、等风险为正；不可只报一种当作『真实』",
                     "静态保证金率非逐日历史对齐（dim updated 2026-10 / cfg 20260324）；futures_rule 档覆盖 2023-01-03..2026-11-02、26/26 品种，2021-22 回退 dim",
                     "未做按权益动态缩放+整数手数+逐日盯市的真实动态复利；固定分数复利隐含无并行冲突、属上界敏感性",
                     "未计资金成本/利息、组合保证金优惠、滑点随规模变化；两腿保证金全额计",
                     "存在单腿未对冲裸露头寸（缺腿），已单列；生产应剔除或补对冲",
                     "并行对数上限与真实容量见 §11，本节只折算已成交样本、不外推",
                 ]},
        "baseline_check": {"n": base.get("n"), "mean_net_R": base.get("mean_net_R"),
                           "PF": base.get("profit_factor"),
                           "simple_sum_return_px": round(simple_sum, 5),
                           "expected": "n=195 / 0.19007 / 1.734 / 1.89933 (§19)"},
        "margin_data": {"n_dim_varieties": len(dim), "n_cfg_margin": len(brk),
                        "n_rate_fallback_legs": n_fallback,
                        "n_missing_legs": len(set(missing)), "missing_symbols": sorted(set(missing)),
                        "b3_varieties": sorted({x["var"] for x in legs})},
        "pair_aggregation": {"n_legs": len(legs), "n_pair_groups": n_groups,
                             "n_incomplete": len(incomplete),
                             "incomplete_sample": [{k: str(v) for k, v in x.items()}
                                                   for x in incomplete[:10]]},
        "naked_single_leg": {"n_groups": len(incomplete),
                             "net_amount": round(naked_amt, 2),
                             "clean_net_amount_excl_naked": round(clean_amt, 2),
                             "note": "单腿组因配对另一腿缺失（数据/停牌）未对冲，方向性暴露；生产上应剔除"},
        "B_margin_return_simple": round(sum(p["ret_on_margin"] for p in pagg
                                             if p["ret_on_margin"] is not None), 5),
        "C_linear_account": main_stats,
        "D_fixed_fraction_compound": comp,
        "E_by_year": by_year,
        "per_pair": [{k: (str(v) if isinstance(v, date) else v) for k, v in p.items()}
                     for p in pagg],
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
