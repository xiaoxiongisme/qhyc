#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""十一期② 换月感知的完整交易日判据（roll-aware L3）

问题：§31 用 daily_bar(888) 校验 60m 聚合日 OHLC，2026 得 complete 119 /
incomplete 12 / absent 58。但 888 是**拼接连续合约**：换月日前后，60m 序列与日线
序列的拼接因子不一定同步，价格水平会突跳 —— 这类偏差不是"数据缺失"，而是
"连续合约构造差异"，用同一个阈值判会把换月日误判成数据问题。

原本的设计（§31.8 ②）是"改用具体合约而非 888 校验"。★ 实测**不可行**：
  l1_mkt.bar_60m / l0_raw.minute_bar / l0_raw.hourly_bar 全表只有 888
  （59/57/59 个 symbol 全部以 888 结尾，非 888 样本为空）；
  l0_raw.daily_bar 虽有 124 个 symbol，但除 FG 历史合约外，每个品种也只有 888；
  public.futures_rule 的 code 是**品种级**（A/AG/...，另含 _O 期权行），不是合约级。
所以"合约级价格校验"在本库做不到。可做且有效的替代是**换月日识别 + 豁免**：
  main_contract_map 逐日给出 (product, main_symbol, underlying, change_flag, delta)，
  换月日 = change_flag 为真 或 underlying 相对前一日变化。

产出：
  A 换月日 vs 非换月日的失配率对比（检验"换月解释假说"）
  B 失配格按品种归因（哪些品种是噪声源、是否与换月频率相关）
  C 换月豁免后的日级判定重算（与 §31 的 strict 口径并列）
  D 传导量化：ATR14（60m 聚合日 vs daily_bar）相对差 —— 直接决定仓位规模

用法：python signal_b11_rollaware.py --start 2024-01-01 --end 2026-11-02
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

from signal_b11_datamon import load_layers, L2_TOL, CLOSE_TOL, L2_RATE, L3_RATE


def load_rolls(session, start: date, end: date) -> dict:
    """{(product, date): {"roll": bool, "main": str}} — 换月日 = change_flag 或 underlying 变化。"""
    rows = session.execute(text("""
        SELECT trade_date, upper(product), main_symbol, underlying, change_flag, delta
        FROM public.main_contract_map
        WHERE trade_date >= :a AND trade_date <= :b
        ORDER BY upper(product), trade_date
    """), {"a": start, "b": end}).all()
    out = {}
    prev = {}
    for d, prod, ms, un, cf, delta in rows:
        cur = str(un or ms or "").upper()
        p = prev.get(prod)
        roll = bool(cf) or (p is not None and cur != p) or (delta not in (None, 0))
        out[(prod, d)] = {"roll": roll, "main": cur, "flag": bool(cf),
                          "delta": float(delta) if delta is not None else None}
        prev[prod] = cur
    return out


def atr14(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=10).mean()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-11-02")
    ap.add_argument("--out", default="/tmp/aeq/b11_rollaware.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=2, max_overflow=4)
    session = sessionmaker(bind=engine)()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    cal, agg, dly, partial = load_layers(session, start, end)
    rolls = load_rolls(session, start, end)

    universe = set(agg["symbol"].unique()) if not agg.empty else set()
    m = agg.merge(dly[dly["symbol"].isin(universe)], on=["symbol", "d"], how="inner",
                  suffixes=("_60", "_d"))
    prod = m["symbol"].str.slice(0, -3).str.upper()
    m["product"] = prod
    m["roll"] = [bool(rolls.get((p, d), {}).get("roll", False))
                 for p, d in zip(m["product"], m["d"])]
    m["roll_flag"] = [bool(rolls.get((p, d), {}).get("flag", False))
                      for p, d in zip(m["product"], m["d"])]

    for c in ("open", "high", "low", "close"):
        a = m[f"{c}_60"].to_numpy(dtype=float)
        b = m[f"{c}_d"].to_numpy(dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            m[f"rel_{c}"] = np.abs(a / b - 1.0)
    m["cell_max"] = m[["rel_open", "rel_high", "rel_low", "rel_close"]].max(axis=1)
    m["bad"] = m["cell_max"] > L2_TOL
    m["bad_close"] = m["rel_close"] > CLOSE_TOL
    m["gross"] = m["cell_max"] > 0.02

    # ---------------- A 换月 vs 非换月 ----------------
    A = {}
    for tag, sub in [("roll", m[m["roll"]]), ("nonroll", m[~m["roll"]])]:
        A[tag] = {
            "n_cells": int(len(sub)),
            "bad_rate": round(float(sub["bad"].mean()), 5) if len(sub) else None,
            "bad_close_rate": round(float(sub["bad_close"].mean()), 5) if len(sub) else None,
            "gross_rate": round(float(sub["gross"].mean()), 5) if len(sub) else None,
            "cell_max_p95_pct": round(float(sub["cell_max"].quantile(0.95)) * 100, 4) if len(sub) else None,
        }
    n_bad = int(m["bad"].sum())
    A["share_of_bad_on_roll"] = round(float((m["bad"] & m["roll"]).sum()) / n_bad, 4) if n_bad else None
    A["share_of_roll_days_bad"] = round(float(m.loc[m["roll"], "bad"].mean()), 4) if m["roll"].any() else None
    A["roll_day_share"] = round(float(m["roll"].mean()), 4)

    # ---------------- B 品种归因 ----------------
    B = []
    for p, sub in m.groupby("product"):
        n_roll = int(sub["roll"].sum())
        B.append({
            "product": p, "n_cells": int(len(sub)),
            "bad": int(sub["bad"].sum()),
            "bad_rate": round(float(sub["bad"].mean()), 4),
            "gross": int(sub["gross"].sum()),
            "n_roll": n_roll,
            "bad_on_roll": int((sub["bad"] & sub["roll"]).sum()),
            "bad_off_roll": int((sub["bad"] & ~sub["roll"]).sum()),
        })
    B.sort(key=lambda x: -x["bad"])
    B = B[:15]

    # ---------------- C 换月豁免后的日级判定 ----------------
    day_rows = []
    for d in cal:
        exp_all = dly[(dly["d"] == d) & (dly["symbol"].isin(universe))]
        exp_nonroll = [s for s in exp_all["symbol"] if not rolls.get(
            (s[:-3].upper(), d), {}).get("roll", False)]
        sub = m[m["d"] == d]
        got = set(sub["symbol"])
        n_exp_all = int(exp_all["symbol"].nunique())
        n_exp_nr = len(exp_nonroll)
        match_all = set(sub.loc[sub["bad"] == False, "symbol"]) if len(sub) else set()
        match_nr = {s for s in match_all if s in set(exp_nonroll)}
        rate_all = len(match_all) / n_exp_all if n_exp_all else 0.0
        rate_nr = len(match_nr) / n_exp_nr if n_exp_nr else 0.0
        is_absent = len(got) == 0
        day_rows.append({
            "date": str(d), "n_exp": n_exp_all, "n_exp_nonroll": n_exp_nr,
            "rate_strict": round(rate_all, 4), "rate_roll_exempt": round(rate_nr, 4),
            "verdict_strict": "absent" if is_absent else (
                "complete" if rate_all >= L2_RATE else
                "complete_calendar" if rate_all >= L3_RATE else "incomplete"),
            "verdict_exempt": "absent" if is_absent else (
                "complete" if rate_nr >= L2_RATE else
                "complete_calendar" if rate_nr >= L3_RATE else "incomplete"),
            "n_roll_syms": n_exp_all - n_exp_nr,
        })

    def tally(key):
        out = {}
        for r in day_rows:
            out[r[key]] = out.get(r[key], 0) + 1
        return out

    fixed = [r for r in day_rows
             if r["verdict_strict"] == "incomplete" and r["verdict_exempt"] == "complete"]
    still = [r for r in day_rows if r["verdict_exempt"] == "incomplete"]

    # ---------------- D ATR 传导 ----------------
    D = []
    for sym, sub in m.groupby("symbol"):
        s60 = sub.sort_values("d").set_index("d")
        a60 = atr14(pd.DataFrame({"high": s60["high_60"], "low": s60["low_60"],
                                  "close": s60["close_60"]}))
        adv = atr14(pd.DataFrame({"high": s60["high_d"], "low": s60["low_d"],
                                  "close": s60["close_d"]}))
        mask = a60.notna() & adv.notna() & (adv > 0)
        if mask.sum() < 30:
            continue
        rel = (a60[mask] / adv[mask] - 1.0).abs()
        D.append({"symbol": sym, "n": int(mask.sum()),
                  "atr_rel_median_pct": round(float(rel.median()) * 100, 4),
                  "atr_rel_p95_pct": round(float(rel.quantile(0.95)) * 100, 4),
                  "atr_rel_max_pct": round(float(rel.max()) * 100, 3)})
    D.sort(key=lambda x: -x["atr_rel_median_pct"])

    all_atr = np.array([d["atr_rel_median_pct"] for d in D], dtype=float)
    out = {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "window": [args.start, args.end], "l2_tol": L2_TOL,
                 "n_cells": int(len(m)), "n_partial_night_tail": partial},
        "A_roll_vs_nonroll": A,
        "B_by_product": B,
        "C_verdict": {
            "strict_counts": tally("verdict_strict"),
            "exempt_counts": tally("verdict_exempt"),
            "n_fixed_by_exemption": len(fixed),
            "fixed_days": [r["date"] for r in fixed],
            "still_incomplete": [{"date": r["date"], "rate": r["rate_roll_exempt"]}
                                 for r in still][:40],
            "n_still_incomplete": len(still),
        },
        "D_atr_transmission": {
            "n_symbols": len(D),
            "median_of_medians_pct": round(float(np.median(all_atr)), 4) if all_atr.size else None,
            "p95_of_medians_pct": round(float(np.percentile(all_atr, 95)), 4) if all_atr.size else None,
            "top15": D[:15],
            "note": "ATR14 分别由 60m 聚合日与 daily_bar 计算；差异直接按比例传导到手数"
                    "（lots = frac*E/ (sl_mult*ATR*mult)）",
        },
        "E_worst_cells": m.sort_values("cell_max", ascending=False).head(20)[
            ["symbol", "d", "open_60", "open_d", "close_60", "close_d", "cell_max",
             "roll", "roll_flag"]].to_dict("records"),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)

    print(f"[cells] n={len(m)}  roll_day_share={A['roll_day_share']:.3f}")
    print(f"[A] roll   bad_rate={A['roll']['bad_rate']:.4f} gross={A['roll']['gross_rate']:.4f} "
          f"p95={A['roll']['cell_max_p95_pct']}%")
    print(f"    nonroll bad_rate={A['nonroll']['bad_rate']:.4f} gross={A['nonroll']['gross_rate']:.4f} "
          f"p95={A['nonroll']['cell_max_p95_pct']}%")
    print(f"    失配格中落在换月日的比例 = {A['share_of_bad_on_roll']}")
    print(f"[B] 失配最多的品种：")
    for b in B[:8]:
        print(f"    {b['product']:>3} bad={b['bad']:>4}/{b['n_cells']:>4} "
              f"({b['bad_rate']:.3f}) gross={b['gross']:>3} roll={b['n_roll']:>3} "
              f"on_roll={b['bad_on_roll']:>3} off_roll={b['bad_off_roll']:>3}")
    print(f"[C] strict={tally('verdict_strict')}")
    print(f"    exempt={tally('verdict_exempt')}  豁免修正 {len(fixed)} 天，仍 incomplete {len(still)} 天")
    print(f"[D] ATR 传导：品种中位数 {out['D_atr_transmission']['median_of_medians_pct']}% "
          f"p95 {out['D_atr_transmission']['p95_of_medians_pct']}%")
    for d in D[:6]:
        print(f"    {d['symbol']:>7} 中位 {d['atr_rel_median_pct']:.3f}% "
              f"p95 {d['atr_rel_p95_pct']:.3f}% max {d['atr_rel_max_pct']:.2f}%")
    print(f"[done] → {args.out}")


if __name__ == "__main__":
    main()
