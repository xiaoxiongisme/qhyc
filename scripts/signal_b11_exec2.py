#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""十一期⑤ 涨跌停判定重做 + 顺延建模；⑥ 保证金真实口径 + 组合保证金敏感性

⑤ 十期的做法（signal_b10_exec.py）有三处可改进：
   (a) 用"开盘涨跌幅 ≥ 98%×涨跌停"作触板代理 —— 会误判"接近但未触板"的强势开盘；
   (b) 只看相对**昨收**的涨跌幅，没看当日是否封住（一字板 / 收盘仍在板上）；
   (c) 被阻断的成交按"整笔剔除"处理，属上界估计；真实是**顺延到下一个可成交日**。
本脚本改为：用 futures_rule 的品种级涨跌幅 × **前结算价**算精确限价，再按
"一字板"（high==low）与"开收均在板"两级判封板，最后实现顺延模型。

★ 关于"改用具体合约"（§31.8 ⑤ 的原设想）：实测**做不到** ——
   l1_mkt/hourly/minute 全表只有 888；daily_bar 每品种除 FG 历史合约外也只有 888；
   futures_rule 的 code 是品种级。故只能品种级判定，这也是交易所实际设板口径。

⑥ 保证金：dim_variety.margin_rate_long/short 与 cfg_symbol_margin 的
   exchange_margin_ratio（交易所）/ broker_margin_ratio（期货公司上浮）并存。
   回测一直用 dim 那一套；若它更接近交易所口径，则**系统性低估 5 个百分点**。
   本脚本用 rate_fn 把 broker 口径注入回测，量保证金占用与爆仓跳过笔数。
   组合保证金（单向大边）官方规则**仍未入库**，只能用 margin_disc 做敏感性，
   结果必须标注为假设。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

from signal_oos_backtest import (WalkForwardParams, daily_atr, load_market,
                                 main_contract_surface, simulate_windows)
from signal_b3_pairs import compute_targets
from signal_b3_compound_oos import PAIRS_ALL
from signal_b3_compound import load_dim
from signal_b3_riskfix import SL_MULT, simulate_fx
from signal_b3_capacity import pair_of

E0 = 1e6
FRAC = 0.02
PRICE_EPS = 1e-6


def code_of(sym: str) -> str:
    return (sym[:-3] if sym.endswith("888") else sym).upper()


def load_rules(session, start: date, end: date):
    """(date, variety) → price_limit(%) ；返回 by_day 与品种中位数回退。"""
    rows = session.execute(text("""
        SELECT trade_date, upper(code), price_limit
        FROM public.futures_rule
        WHERE price_limit IS NOT NULL AND trade_date >= :a AND trade_date <= :b
    """), {"a": start, "b": end}).all()
    by_day, hist = {}, defaultdict(list)
    for d, code, p in rows:
        p = float(p)
        if p <= 0 or p > 40:
            continue
        for c in str(code).split("/"):
            by_day[(d, c)] = p
            hist[c].append(p)
    fb = {k: float(np.median(v)) for k, v in hist.items()}
    return by_day, fb


def load_daily(engine, session, symbols, start, end):
    """888 日线 OHLC + settle（含区间前 10 天，用于取前结算价）。"""
    rows = session.execute(text("""
        SELECT symbol, trade_date, open, high, low, close, settle
        FROM l0_raw.daily_bar
        WHERE symbol = ANY(:syms) AND trade_date >= :a AND trade_date <= :b
        ORDER BY symbol, trade_date
    """), {"syms": symbols, "a": start - timedelta(days=15), "b": end}).all()
    df = pd.DataFrame(rows, columns=["symbol", "d", "open", "high", "low", "close", "settle"])
    for c in ("open", "high", "low", "close", "settle"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_margin_cfg(session):
    rows = session.execute(text("""
        SELECT upper(variety_code), exchange_margin_ratio, broker_margin_ratio
        FROM public.cfg_symbol_margin
    """)).all()
    ex, br = {}, {}
    for v, a, b in rows:
        if a is not None:
            ex[v] = float(a)
        if b is not None:
            br[v] = float(b)
    return ex, br


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/aeq/b11_exec2.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in get_settings().main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    dim = load_dim(session)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)

    o, c, h, l = load_market(session, symbols, start - timedelta(days=400), end,
                             calendar_mode="weekend_drop")
    atr = daily_atr(h, l, c)
    cmap = main_contract_surface(session, symbols, list(o.index))
    tgt, _ = compute_targets(c, pairs=PAIRS_ALL, window=60, entry_z=2.0, exit_z=0.5,
                             return_strength=True)
    t = tgt.reindex(index=o.index, columns=symbols).fillna(0.0)
    trades = simulate_windows(t, o, c, atr, wfp, start, end, rebalance_n=1,
                              sl_mult=SL_MULT, contract_map=cmap, coef_cache={})
    cal = [d for d in o.index if start <= (d if isinstance(d, date) else d.date()) <= end]
    print(f"[init] n_trades={len(trades)}", flush=True)

    opens = o.copy()
    opens.index = [str(x)[:10] for x in opens.index]
    dates = list(opens.index)
    di = {d: i for i, d in enumerate(dates)}

    daily = load_daily(engine, session, symbols, start, end)
    dmap = {(r["symbol"], str(r["d"])): r for _, r in daily.iterrows()}
    # 前结算价（按 symbol 的日线顺序）
    daily = daily.sort_values(["symbol", "d"])
    daily["prev_settle"] = daily.groupby("symbol")["settle"].shift(1)
    psmap = {(r["symbol"], str(r["d"])): r["prev_settle"] for _, r in daily.iterrows()}
    by_day, fb = load_rules(session, start, end)

    out = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "start": args.start, "end": args.end, "frac": FRAC, "e0": E0,
                    "price_eps": PRICE_EPS}}

    # ================= ⑤ 涨跌停 =================
    print("\n[⑤] 涨跌停：精确限价 + 封板分层", flush=True)
    rows, t1, t2 = [], [], []
    n_no_rule = 0
    for t_ in trades:
        sym = t_["symbol"]; var = code_of(sym)
        ed, xd = str(t_["entry_dt"])[:10], str(t_["exit_dt"])[:10]
        rec = {"symbol": sym, "side": t_["side"], "entry": ed, "exit": xd,
               "net_R": round(float(t_["net"]), 4), "blocks": []}
        for leg, d in (("entry", ed), ("exit", xd)):
            bar = dmap.get((sym, d))
            ps = psmap.get((sym, d))
            pct = by_day.get((date.fromisoformat(d), var), fb.get(var))
            if bar is None or ps is None or pct is None or not np.isfinite(ps):
                if pct is None:
                    n_no_rule += 1
                continue
            up = ps * (1 + pct / 100.0)
            dn = ps * (1 - pct / 100.0)
            one_word = abs(float(bar["high"]) - float(bar["low"])) <= PRICE_EPS
            at_up = abs(float(bar["open"]) - up) <= PRICE_EPS * max(1.0, up)
            at_dn = abs(float(bar["open"]) - dn) <= PRICE_EPS * max(1.0, dn)
            cl_up = abs(float(bar["close"]) - up) <= PRICE_EPS * max(1.0, up)
            cl_dn = abs(float(bar["close"]) - dn) <= PRICE_EPS * max(1.0, dn)
            rec[f"{leg}_pct"] = pct
            rec[f"{leg}_one_word"] = bool(one_word)
            rec[f"{leg}_locked_up"] = bool(one_word and cl_up)
            rec[f"{leg}_locked_dn"] = bool(one_word and cl_dn)
            rec[f"{leg}_open_at_up"] = bool(at_up)
            rec[f"{leg}_open_at_dn"] = bool(at_dn)
            rec[f"{leg}_close_at_up"] = bool(cl_up)
            rec[f"{leg}_close_at_dn"] = bool(cl_dn)
            rec[f"{leg}_move_pct"] = round((float(bar["open"]) / ps - 1.0) * 100.0, 3)
            rec[f"{leg}_ratio"] = round(abs(float(bar["open"]) / ps - 1.0) * 100.0 / pct, 4)
            # 分级：T1 一字板（high==low 且收在板）｜T2 开收均在板
            blocked = False
            if leg == "entry":
                bad = (t_["side"] == "LONG" and (rec[f"{leg}_locked_up"] or
                                                 (at_up and cl_up))) or \
                      (t_["side"] == "SHORT" and (rec[f"{leg}_locked_dn"] or
                                                  (at_dn and cl_dn)))
                tier = "T1" if ((t_["side"] == "LONG" and rec[f"{leg}_locked_up"]) or
                                (t_["side"] == "SHORT" and rec[f"{leg}_locked_dn"])) else "T2"
            else:
                bad = (t_["side"] == "LONG" and (rec[f"{leg}_locked_dn"] or
                                                 (at_dn and cl_dn))) or \
                      (t_["side"] == "SHORT" and (rec[f"{leg}_locked_up"] or
                                                  (at_up and cl_up)))
                tier = "T1" if ((t_["side"] == "LONG" and rec[f"{leg}_locked_dn"]) or
                                (t_["side"] == "SHORT" and rec[f"{leg}_locked_up"])) else "T2"
            if bad:
                blocked = True
                rec[f"{leg}_tier"] = tier
                rec["blocks"].append(f"{leg}:{tier}")
        rows.append(rec)
        if any(b.startswith("entry") for b in rec["blocks"]):
            t1.append(t_)
        if rec["blocks"]:
            t2.append(t_)

    blocked = t2
    print(f"   成交 {len(trades)} 笔；开仓被阻断 {len(t1)} 笔；任一腿被阻断 {len(blocked)} 笔 "
          f"({len(blocked)/max(len(trades),1):.1%})；无涨跌幅规则 {n_no_rule} 次")

    # 顺延模型
    def defer_one(t_, max_try=3):
        """返回 (new_entry_dt, new_entry_px, new_exit_dt, new_exit_px) 或 None(放弃)。"""
        sym = t_["symbol"]; var = code_of(sym)
        ed, xd = str(t_["entry_dt"])[:10], str(t_["exit_dt"])[:10]
        def can_fill(d, leg):
            bar = dmap.get((sym, d)); ps = psmap.get((sym, d))
            pct = by_day.get((date.fromisoformat(d), var), fb.get(var))
            if bar is None or ps is None or pct is None:
                return True
            up, dn = ps * (1 + pct / 100.0), ps * (1 - pct / 100.0)
            ow = abs(float(bar["high"]) - float(bar["low"])) <= PRICE_EPS
            o_, cl_ = float(bar["open"]), float(bar["close"])
            at_up = abs(o_ - up) <= PRICE_EPS * max(1.0, up)
            at_dn = abs(o_ - dn) <= PRICE_EPS * max(1.0, dn)
            cl_up = abs(cl_ - up) <= PRICE_EPS * max(1.0, up)
            cl_dn = abs(cl_ - dn) <= PRICE_EPS * max(1.0, dn)
            if leg == "entry":
                if t_["side"] == "LONG":
                    return not ((ow and cl_up) or (at_up and cl_up))
                return not ((ow and cl_dn) or (at_dn and cl_dn))
            if t_["side"] == "LONG":
                return not ((ow and cl_dn) or (at_dn and cl_dn))
            return not ((ow and cl_up) or (at_up and cl_up))
        ei = di.get(ed); xi = di.get(xd)
        if ei is None or xi is None:
            return None
        for k in range(1, max_try + 1):
            if ei + k >= len(dates):
                return None
            nd = dates[ei + k]
            if can_fill(nd, "entry"):
                new_e = nd
                break
        else:
            return None
        new_x = xd
        for k in range(1, max_try + 1):
            if can_fill(new_x, "exit"):
                break
            if xi + k >= len(dates):
                return None
            new_x = dates[xi + k]
        e_px = float(opens.loc[new_e, sym])
        x_px = float(opens.loc[new_x, sym])
        if not np.isfinite(e_px) or not np.isfinite(x_px):
            return None
        return {"entry_dt": new_e, "entry_px": e_px, "exit_dt": new_x, "exit_px": x_px}

    def rebuild(t_, p):
        sign = 1.0 if t_["side"] == "LONG" else -1.0
        pnl = sign * (p["exit_px"] - p["entry_px"])
        return {**t_, **p, "pnl": pnl, "gross": pnl / t_["risk"],
                "net": (pnl - t_["cost_pts"]) / t_["risk"]}

    deferred, failed = [], 0
    for t_ in trades:
        if t_ in blocked:
            p = defer_one(t_)
            if p is None:
                failed += 1
                continue
            deferred.append(rebuild(t_, p))
        else:
            deferred.append(t_)

    def nav_of(trs):
        nav, _c, _e, dg, _l = simulate_fx(trs, c, cal, dim, FRAC, "risk", e0=E0, mtm=True)
        ret = np.asarray(nav, float) / E0
        peak = np.maximum.accumulate(ret)
        return {"n_trades": len(trs), "nav": round(float(ret[-1]), 6),
                "mdd": round(float((1 - ret / peak).max()), 6),
                "net_R": round(float(sum(x["net"] for x in trs)), 4)}

    base_nav = nav_of(trades)
    drop_nav = nav_of([x for x in trades if x not in blocked])
    def_nav = nav_of(deferred)
    blocked_net = sum(float(x["net"]) for x in blocked)
    print(f"   基线 {base_nav}")
    print(f"   剔除阻断 {drop_nav}")
    print(f"   顺延     {def_nav}（顺延失败 {failed} 笔）")
    print(f"   被阻断笔原值合计 {blocked_net:+.3f}R")
    for r in rows:
        if r["blocks"]:
            print(f"     {r['entry']}→{r['exit']} {r['symbol']:>7} {r['side']:>5} "
                  f"{r['blocks']} netR={r['net_R']:+.3f}")

    out["limit"] = {"n_trades": len(trades), "n_block_entry": len(t1),
                    "n_block_any": len(blocked),
                    "n_no_rule": int(n_no_rule),
                    "blocked_net_R": round(blocked_net, 4),
                    "baseline": base_nav, "after_drop": drop_nav,
                    "after_defer": def_nav, "n_defer_failed": failed,
                    "detail": [r for r in rows if r["blocks"]]}

    # 反证：若"无阻断"成立，则所有腿的 |开盘相对前结算| 必然 < 涨跌幅
    ratios = []
    for r in rows:
        for leg in ("entry", "exit"):
            if f"{leg}_ratio" in r:
                ratios.append((r[f"{leg}_ratio"], r["symbol"], r[f"{leg}_move_pct"],
                               r.get(f"{leg}_pct"), r[leg]))
    ratios.sort(reverse=True)
    n_close_at_limit = sum(1 for r in rows for leg in ("entry", "exit")
                           if r.get(f"{leg}_close_at_up") or r.get(f"{leg}_close_at_dn"))
    n_one_word = sum(1 for r in rows for leg in ("entry", "exit")
                     if r.get(f"{leg}_one_word"))
    n_rule_violated = sum(1 for x in ratios if x[0] > 1.0)
    out["limit"]["n_one_word_legs"] = n_one_word
    out["limit"]["n_rule_violated_legs"] = n_rule_violated
    out["limit"]["note"] = (
        f"{n_rule_violated}/{len(ratios)} 条腿出现 |开盘位移|>涨跌幅，说明 futures_rule.price_limit "
        "在剧烈行情日**低估**（如 FU 2026-03-10 记 7% 而实际开盘 +8.43%、次日才上调到 20%）；"
        "本库无合约级价格、无逐日准确的涨跌幅，故涨跌停只能给**下界**：一字板腿数即硬阻断数。")
    out["limit"]["closest_to_board"] = [
        {"ratio": x[0], "symbol": x[1], "move_pct": x[2], "limit_pct": x[3], "leg": x[4]}
        for x in ratios[:8]]
    out["limit"]["n_legs"] = len(ratios)
    out["limit"]["n_close_at_limit_legs"] = n_close_at_limit
    out["limit"]["max_ratio"] = ratios[0][0] if ratios else None
    print(f"   反证：{len(ratios)} 条腿中，开盘相对前结算的最大位移 / 涨跌幅 = "
          f"{ratios[0][0] if ratios else None}（>1 才可能触板）")
    for x in ratios[:5]:
        print(f"      {x[1]:>7} {x[4]} 开盘位移 {x[2]:+.2f}% / 限 {x[3]}% → 比值 {x[0]:.3f}")
    print(f"   收盘停在板上的腿数（封板天）= {n_close_at_limit}；一字板腿数 = {n_one_word}")
    print(f"   涨跌幅规则被行情证伪的腿数 = {n_rule_violated}/{len(ratios)}")

    # ================= ⑥ 保证金 =================
    print("\n[⑥] 保证金口径", flush=True)
    ex, br = load_margin_cfg(session)
    comp = []
    for var, d_ in dim.items():
        mult, rl, rs = d_
        e = ex.get(var); b = br.get(var)
        if e and rl:
            comp.append({"variety": var, "dim_long": round(rl, 4), "cfg_exchange": e,
                         "cfg_broker": b, "dim_over_exchange": round(rl / e, 3),
                         "broker_over_dim": round((b or rl) / rl, 3)})
    ratios = [x["dim_over_exchange"] for x in comp if x["dim_over_exchange"]]
    br_ratios = [x["broker_over_dim"] for x in comp if x["broker_over_dim"]]
    print(f"   dim/exchange 中位={np.median(ratios):.3f}  n={len(ratios)}")
    print(f"   broker/dim  中位={np.median(br_ratios):.3f}  n={len(br_ratios)}")

    def make_rate_fn(source):
        def fn(var, ds, side):
            if source == "cfg_broker":
                v = br.get(var)
                if v:
                    return v
            if source == "cfg_exchange":
                v = ex.get(var)
                if v:
                    return v
            return None
        return fn

    margin_runs = {}
    for source in ("dim", "cfg_exchange", "cfg_broker"):
        for disc in (1.0, 0.8, 0.6):
            nav, _c, ev, dg, _l = simulate_fx(
                trades, c, cal, dim, FRAC, "risk", e0=E0, mtm=True,
                rate_fn=make_rate_fn(source) if source != "dim" else None,
                margin_disc=disc, enforce_margin=True)
            ret = np.asarray(nav, float) / E0
            peak = np.maximum.accumulate(ret)
            kinds = defaultdict(int)
            for e_ in ev:
                kinds[e_["kind"]] += 1
            key = f"{source}|disc={disc}"
            margin_runs[key] = {
                "nav": round(float(ret[-1]), 6),
                "mdd": round(float((1 - ret / peak).max()), 6),
                "margin_util_max": dg.get("margin_util_max"),
                "n_breach": kinds["margin_breach"],
                "n_skipped": kinds["skipped_by_margin"],
            }
            print(f"   {key:<22} nav={ret[-1]:.4f} mdd={margin_runs[key]['mdd']:.4f} "
                  f"util={dg.get('margin_util_max')} breach={kinds['margin_breach']} "
                  f"skip={kinds['skipped_by_margin']}")

    out["margin"] = {
        "dim_vs_cfg": {
            "n": len(comp),
            "dim_over_exchange_median": round(float(np.median(ratios)), 4) if ratios else None,
            "broker_over_dim_median": round(float(np.median(br_ratios)), 4) if br_ratios else None,
            "detail": comp[:40],
        },
        "runs": margin_runs,
        "note": "组合保证金（单向大边）官方规则未入库；disc=0.8/0.6 为**假设**敏感性，"
                "仅改变保证金占用判定，不改变手数。dim 源未传 rate_fn。",
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
    print(f"[done] → {args.out}")


if __name__ == "__main__":
    main()
