"""十期② bar 标签口径统一 + 入口断言 + 影响实证

背景（§30.3 R1）：bar_60m 的 bucket 语义在 2026-11-02 当天由「右端+会话锚定」切为「左端+整点」。
§30 只指出"实时链路可能引入 1 小时前视"，本轮把三件事做掉：

  A 口径检测：逐日判定 left/right，给出切换日（可复用的入口断言 detect_bar_convention）
  B 重聚合：由 bar_5m 重建「左端整点」60m（单一口径），与 native 派生的日 OHLC 对比
  C 影响：用同一套 B3 信号链分别吃 native / reagg5，比较成交数、agg_net、逐年 R

关键前提（本轮新认知）：B3 的信号与模拟都跑在**日线**上，60m 只用来派生日 OHLC。
因此口径切换对回测的影响应接近 0 —— 它是**运维/实时风险**，不是研究结论风险。
本脚本就是来证伪或证实这一点，而不是假设它。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

from signal_oos_backtest import (WalkForwardParams, _reagg_60m_from_5m, daily_atr,
                                 detect_bar_convention, load_market,
                                 main_contract_surface, simulate_windows)
from signal_b3_pairs import compute_targets
from signal_b3_compound_oos import PAIRS_ALL
from signal_b3_riskfix import SL_MULT, load_dim  # noqa: F401

OUT_TOL = 0.002   # 日 OHLC 相对偏差容忍


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    net = np.array([t["net"] for t in trades], dtype=float)
    years = {}
    for t in trades:
        y = str(t["entry_dt"])[:4]
        years.setdefault(y, {"n": 0, "net": 0.0})
        years[y]["n"] += 1
        years[y]["net"] += float(t["net"])
    return {"n": len(trades), "agg_net": round(float(net.sum()), 5),
            "mean": round(float(net.mean()), 5), "win": round(float((net > 0).mean()), 4),
            "by_year": {k: {"n": v["n"], "net": round(v["net"], 5)} for k, v in sorted(years.items())}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--detect-end", default="2026-11-03")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--out", default="/tmp/aeq/b10_caliber.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in get_settings().main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    out: dict = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                          "window": [args.start, args.end], "bar_modes": ["native", "reagg5"]}}

    # ---------------- A 口径检测 ----------------
    print("[A] 口径检测（60m 是否存在 hour=9 的日盘首根）", flush=True)
    import time as _t
    _t0 = _t.time()
    det = detect_bar_convention(session, start - timedelta(days=40), date.fromisoformat(args.detect_end))
    print(f"   left 日数={det['n_left']} right 日数={det['n_right']} "
          f"切换日={det['switch_date']}")
    print(f"   left 日: {[str(d) for d in det['left'][:5]]} ... {[str(d) for d in det['left'][-3:]]}")
    out["A_convention"] = {"n_left": det["n_left"], "n_right": det["n_right"],
                           "switch_date": str(det["switch_date"]) if det["switch_date"] else None,
                           "left_days_head": [str(d) for d in det["left"][:5]],
                           "left_days_tail": [str(d) for d in det["left"][-3:]],
                           "assertion_rule": "日盘存在 09:00 bar → left（左端）；否则 right（右端）",
                           "realtime_risk": "right 口径下 bar 的 bucket 是结束时刻；若把 bucket 当决策时点，"
                                            "等于提前 1 小时拿到收盘价 → 前视"}

    # ---------------- B 重聚合一致性 ----------------
    print("\n[B] 5m→左端整点 60m 重聚合，与 native 派生日 OHLC 对比")
    b_start = end - timedelta(days=330)
    print(f"   （B 段窗口 {b_start} → {end}）")
    frame_b = _reagg_60m_from_5m(session, b_start - timedelta(days=400), end)
    print(f"   [t] 帧构建 {_t.time()-_t0:.0f}s", flush=True)
    print(f"   reagg 帧行数={len(frame_b):,}")
    cells = {}
    for cm in ("weekend_drop", "session_map"):
        o_n, c_n, h_n, l_n = load_market(session, symbols, b_start - timedelta(days=400), end,
                                         calendar_mode=cm, bar_mode="native")
        o_r, c_r, h_r, l_r = load_market(session, symbols, b_start - timedelta(days=400), end,
                                         calendar_mode=cm, bar_mode="reagg5", bar_frame=frame_b)
        common_idx = o_n.index.intersection(o_r.index)
        common_col = o_n.columns.intersection(o_r.columns)
        res = {"n_days_native": len(o_n), "n_days_reagg": len(o_r),
               "common_days": len(common_idx), "common_syms": len(common_col)}
        for name, a, b in (("close", c_n, c_r), ("open", o_n, o_r),
                           ("high", h_n, h_r), ("low", l_n, l_r)):
            aa = a.loc[common_idx, common_col].to_numpy(dtype=float)
            bb = b.loc[common_idx, common_col].to_numpy(dtype=float)
            m = np.isfinite(aa) & np.isfinite(bb) & (aa != 0)
            if not m.any():
                continue
            dev = np.abs(aa[m] - bb[m]) / np.abs(aa[m])
            res[name] = {"n": int(m.sum()), "median_dev": round(float(np.median(dev)), 8),
                         "p99_dev": round(float(np.percentile(dev, 99)), 6),
                         "max_dev": round(float(dev.max()), 6),
                         "share_over_tol": round(float((dev > OUT_TOL).mean()), 6)}
        cells[cm] = res
        print(f"   [{cm}] days={res['common_days']} close: med={res['close']['median_dev']:.2e} "
              f"p99={res['close']['p99_dev']:.2e} max={res['close']['max_dev']:.2e} "
              f"超容忍={res['close']['share_over_tol']:.4%} | open max={res['open']['max_dev']:.2e}")
    out["B_reagg"] = {"tolerance": OUT_TOL, "by_calendar_mode": cells,
                      "note": "close/open/high/low 均由 60m 派生；口径只影响'哪根 bar 是当日最后一根'，"
                              "左端整点与右端会话锚定在收盘价上应等价（除零成交边界 bar）"}

    # ---------------- C 回测影响 ----------------
    print("\n[C] B3 信号链 native vs reagg5（calendar_mode=weekend_drop）")
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    runs = {}
    print(f"   构建全区间 reagg 帧（一次性，较慢）... [t] B 段累计 {_t.time()-_t0:.0f}s", flush=True)
    frame_c = _reagg_60m_from_5m(session, start - timedelta(days=400), end)
    print(f"   帧行数={len(frame_c):,}")
    for bm in ("native", "reagg5"):
        o, c, h, l = load_market(session, symbols, start - timedelta(days=400), end,
                                 calendar_mode="weekend_drop", bar_mode=bm,
                                 bar_frame=(frame_c if bm == "reagg5" else None))
        atr = daily_atr(h, l, c)
        cmap = main_contract_surface(session, symbols, list(o.index))
        tgt, _ = compute_targets(c, pairs=PAIRS_ALL, window=args.window,
                                 entry_z=2.0, exit_z=0.5, return_strength=True)
        t = tgt.reindex(index=o.index, columns=symbols).fillna(0.0)
        trades = simulate_windows(t, o, c, atr, wfp, start, end,
                                  rebalance_n=1, sl_mult=SL_MULT,
                                  contract_map=cmap, coef_cache={})
        s = summarize(trades)
        s["n_calendar"] = len([d for d in o.index if start <= (d if isinstance(d, date) else d.date()) <= end])
        runs[bm] = s
        print(f"   {bm}: n_trades={s['n']} agg_net={s['agg_net']} mean={s['mean']} win={s['win']:.3f}")

    a, b = runs["native"], runs["reagg5"]
    delta = {"n_trades": b["n"] - a["n"], "agg_net": round(b["agg_net"] - a["agg_net"], 5)}
    yrs = sorted(set(a.get("by_year", {})) | set(b.get("by_year", {})))
    delta["by_year"] = {y: round(b.get("by_year", {}).get(y, {}).get("net", 0.0)
                                 - a.get("by_year", {}).get(y, {}).get("net", 0.0), 5) for y in yrs}
    verdict = ("口径对回测结论无影响（逐笔集合与净 R 一致）"
               if delta["n_trades"] == 0 and abs(delta["agg_net"]) < 1e-6
               else "口径对回测有实质影响，需统一后重算")
    print(f"   Δ: trades={delta['n_trades']:+d} agg_net={delta['agg_net']:+.5f} → {verdict}")
    out["C_backtest"] = {"runs": runs, "delta": delta, "verdict": verdict,
                         "why": "B3 的信号与模拟均在日线上；60m 只派生日 OHLC，"
                                "故标签口径只改'哪根是当日末根'，不改日线价格序列"}

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n[done] → {args.out}")


if __name__ == "__main__":
    main()
