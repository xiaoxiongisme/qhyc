"""十期⑤ 可执行性检验（涨跌停 / 容量）+ session_map 复利对照

三项，都是 B3 至今**完全未建模**的失效来源：

A 涨跌停：用 public.futures_rule.price_limit（百分数，业界 0.5–25%）判断成交日开盘是否触板。
  方向性判断（不是"有涨跌停就废"）：
    LONG  开仓 遇开盘涨停 → 买不到；SHORT 开仓 遇开盘跌停 → 卖不出
    LONG  平仓 遇开盘跌停 → 卖不出；SHORT 平仓 遇开盘涨停 → 买不回
  被阻断的成交按"整笔剔除"近似（真实应为顺延，属上界估计），报剔除后的收益变化。

B 容量：用复利引擎给出的手数算名义额，除以当日成交额（daily_bar 888 的量 × 价格 × 乘数），
  得参与率。注意 daily_bar 888 的量是**品种级**（§31 已证非主力合约），故参与率被低估 = 偏乐观。

C session_map 复利对照：六期只做了门槛，没做复利；此处补 weekend_drop vs session_map。
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
from signal_b10_mdd import FRAC, K_STAR, E0, metrics

LIMIT_BUFFER = 0.98   # 开盘涨跌幅达到涨跌停的 98% 即视为触板


def code_of(sym: str) -> str:
    return sym[:-3] if sym.endswith("888") else sym


def load_limits(session, start: date, end: date) -> dict:
    rows = session.execute(text("""
        SELECT trade_date, code, price_limit FROM public.futures_rule
        WHERE trade_date >= :a AND trade_date <= :b AND price_limit IS NOT NULL
    """), {"a": start, "b": end}).all()
    lim = {}
    for d, code, p in rows:
        lim[(d, str(code).upper())] = float(p)
    # 回退：该品种 2023 前的众数
    modal = defaultdict(list)
    for (_d, code), p in lim.items():
        modal[code].append(p)
    fallback = {k: float(np.median(v)) for k, v in modal.items()}
    return {"by_day": lim, "fallback": fallback}


def limit_for(lim: dict, d: date, code: str) -> float | None:
    if (d, code) in lim["by_day"]:
        return lim["by_day"][(d, code)]
    return lim["fallback"].get(code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/aeq/b10_exec.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in get_settings().main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    dim = load_dim(session)

    def build(cm: str):
        o, c, h, l = load_market(session, symbols, start - timedelta(days=400), end,
                                 calendar_mode=cm)
        atr = daily_atr(h, l, c)
        cmap = main_contract_surface(session, symbols, list(o.index))
        tgt, _ = compute_targets(c, pairs=PAIRS_ALL, window=60, entry_z=2.0, exit_z=0.5,
                                 return_strength=True)
        t = tgt.reindex(index=o.index, columns=symbols).fillna(0.0)
        tr = simulate_windows(t, o, c, atr, wfp, start, end, rebalance_n=1,
                              sl_mult=SL_MULT, contract_map=cmap, coef_cache={})
        cal = [d for d in o.index if start <= (d if isinstance(d, date) else d.date()) <= end]
        return tr, c, cal

    print("[init] weekend_drop 建仓 ...", flush=True)
    trades, closes, calendar = build("weekend_drop")
    print(f"   n_trades={len(trades)}", flush=True)

    out = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "limit_buffer": LIMIT_BUFFER, "frac": FRAC, "e0": E0}}

    # ---------------- A 涨跌停 ----------------
    print("\n[A] 涨跌停可执行性", flush=True)
    lim = load_limits(session, start, end)
    cl = closes.copy()
    cl.index = [str(x)[:10] for x in cl.index]
    dates = list(cl.index)
    di = {d: i for i, d in enumerate(dates)}

    def prev_close(d: str, sym: str):
        i = di.get(d)
        if i is None or i == 0:
            return None
        v = cl.iloc[i - 1].get(sym)
        return float(v) if v == v else None

    blocked, rows = [], []
    for t in trades:
        sym = t["symbol"]; code = code_of(sym).upper()
        ed, xd = str(t["entry_dt"])[:10], str(t["exit_dt"])[:10]
        e_move = x_move = None
        pc_e = prev_close(ed, sym)
        if pc_e:
            e_move = (float(t["entry_px"]) / pc_e - 1.0) * 100.0
        pc_x = prev_close(xd, sym)
        if pc_x:
            x_move = (float(t["exit_px"]) / pc_x - 1.0) * 100.0
        le = limit_for(lim, date.fromisoformat(ed), code)
        lx = limit_for(lim, date.fromisoformat(xd), code)
        why = []
        if le and e_move is not None:
            thr = le * LIMIT_BUFFER
            if t["side"] == "LONG" and e_move >= thr:
                why.append("LONG开仓遇涨停")
            if t["side"] == "SHORT" and e_move <= -thr:
                why.append("SHORT开仓遇跌停")
        if lx and x_move is not None:
            thr = lx * LIMIT_BUFFER
            if t["side"] == "LONG" and x_move <= -thr:
                why.append("LONG平仓遇跌停")
            if t["side"] == "SHORT" and x_move >= thr:
                why.append("SHORT平仓遇涨停")
        rows.append({"symbol": sym, "side": t["side"], "entry": ed, "exit": xd,
                     "e_move_pct": round(e_move, 3) if e_move is not None else None,
                     "x_move_pct": round(x_move, 3) if x_move is not None else None,
                     "limit_pct": le, "net_R": round(float(t["net"]), 4),
                     "blocked": why})
        if why:
            blocked.append(t)

    print(f"   总成交 {len(trades)} 笔，触板阻断 {len(blocked)} 笔 "
          f"({len(blocked)/max(len(trades),1):.1%})")
    for b in rows:
        if b["blocked"]:
            print(f"     {b['entry']}→{b['exit']} {b['symbol']:>7} {b['side']:>5} "
                  f"开{b['e_move_pct']:+.2f}% 平{b['x_move_pct']:+.2f}% "
                  f"限{b['limit_pct']:.1f}% {b['blocked']} netR={b['net_R']:+.3f}")

    base = metrics(trades, closes, calendar, dim, FRAC, E0, k=None)
    ex = [t for t in trades if t not in blocked]
    after = metrics(ex, closes, calendar, dim, FRAC, E0, k=None)
    blocked_net = sum(float(t["net"]) for t in blocked)
    print(f"   基线 nav={base['nav']:.4f} netR={base['net_R']:.3f} → "
          f"剔除阻断 nav={after['nav']:.4f} netR={after['net_R']:.3f} "
          f"（被阻断笔贡献 {blocked_net:+.3f}R）")
    out["A_limit"] = {"n_trades": len(trades), "n_blocked": len(blocked),
                      "blocked_share": round(len(blocked) / max(len(trades), 1), 4),
                      "blocked_net_R": round(blocked_net, 4),
                      "baseline": base, "after_drop": after,
                      "blocked_detail": [b for b in rows if b["blocked"]],
                      "note": "整笔剔除是上界估计；真实应顺延到下一可成交日"}

    # ---------------- B 容量 ----------------
    print("\n[B] 容量（参与率）", flush=True)
    nav, _c, _e, diag, lots = simulate_fx(trades, closes, calendar, dim, FRAC, "risk",
                                          e0=E0, mtm=True)
    vols = session.execute(text("""
        SELECT symbol, trade_date, volume FROM l0_raw.daily_bar
        WHERE trade_date >= :a AND trade_date <= :b AND symbol LIKE '%888'
    """), {"a": start, "b": end}).all()
    volmap = {(d, s): int(v or 0) for s, d, v in vols}
    parts = []
    for t, lt in zip(trades, lots if lots else [1] * len(trades)):
        sym = t["symbol"]; d = str(t["entry_dt"])[:10]
        mult = dim[code_of(sym).upper()][0]
        notional = float(lt) * float(t["entry_px"]) * mult
        v = volmap.get((date.fromisoformat(d), sym), 0)
        mkt = v * float(t["entry_px"]) * mult
        if mkt > 0:
            parts.append({"symbol": sym, "date": d, "lots": float(lt),
                          "notional": notional, "market_notional": mkt,
                          "participation": notional / mkt})
    if parts:
        pr = np.array([p["participation"] for p in parts], dtype=float)
        print(f"   参与率: n={len(pr)} 中位={np.median(pr):.2e} p95={np.percentile(pr,95):.2e} "
              f"最大={pr.max():.2e}")
        top = sorted(parts, key=lambda x: -x["participation"])[:5]
        for x in top:
            print(f"     最大 {x['symbol']:>7} {x['date']} lots={x['lots']:.1f} "
                  f"名义={x['notional']:,.0f} 市场={x['market_notional']:,.0f} "
                  f"参与={x['participation']:.2%}")
        out["B_capacity"] = {"n": len(pr), "median": float(np.median(pr)),
                             "p95": float(np.percentile(pr, 95)), "max": float(pr.max()),
                             "top5": top,
                             "note": "分母用 daily_bar 888 的品种级成交量 → 参与率偏乐观（低估）"}

    # ---------------- C session_map 复利对照 ----------------
    print("\n[C] session_map vs weekend_drop 复利", flush=True)
    tr2, c2, cal2 = build("session_map")
    m2 = metrics(tr2, c2, cal2, dim, FRAC, E0, k=None)
    m2k = metrics(tr2, c2, cal2, dim, FRAC, E0, k=K_STAR)
    print(f"   weekend_drop: n={base['n_trades']} nav={base['nav']:.4f} mdd={base['mdd_peak']:.4f}")
    print(f"   session_map : n={m2['n_trades']} nav={m2['nav']:.4f} mdd={m2['mdd_peak']:.4f}"
          f" | k*=5 nav={m2k['nav']:.4f} mdd={m2k['mdd_peak']:.4f}")
    out["C_session_map"] = {"weekend_drop": base, "session_map": m2, "session_map_k5": m2k,
                            "delta_nav": round(m2["nav"] - base["nav"], 6),
                            "delta_mdd": round(m2["mdd_peak"] - base["mdd_peak"], 6)}

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"[done] → {args.out}")


if __name__ == "__main__":
    main()
