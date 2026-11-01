#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2 执行层入场研究（2026-11-01）。

不改小时信号/离场序列，只改变入场成交：
- MKT：信号 bar 收盘后，以下一根 5m 开盘价成交（可实现基准）。
- 限价：signal_close - q*ATR（多头；空头镜像），等待 3/6/12 根 5m。
  fallback=超时后以一根 5m 开盘追入；skip=超时放弃。
- 原持仓在限价单成交前结束时，挂单撤销（记 cancelled，不计成交）。

评价以实现 MKT 为基准：filled 的改进 = dir*(mkt_px-fill_px)/(2*ATR)；
missed/cancelled 的改进 = -R_mkt（完整机会成本）。这是事件级筛选，未模拟
入场延迟对止损/加码/后续信号路径的二阶影响；通过后才允许进入影子执行。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/tmp/eg_run")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings
from app.backtest.fusion_backtest import fusion_params_from_config, _read_hourly, _htf_direction
from app.strategies.fusion_signal import walk_fusion_states

IS_END = pd.Timestamp("2021-01-01", tz="Asia/Shanghai")
QS = (0.0, 0.10, 0.25, 0.50)
HS = (3, 6, 12)


def trades_from_engine(o, h, l, c, p):
    """与 study3 相同的首仓序列；只保留执行研究需要的价格/时间索引。"""
    out = []
    open_pos = None
    for kgen, d in enumerate(walk_fusion_states(o, h, l, c, _htf_direction(c, int(p.ema_k)), p)):
        i = kgen + 2
        st = d["state"]
        if open_pos is not None and (st == 0 or st != open_pos["dir_state"]):
            e = open_pos
            out.append({**e, "exit_i": i, "exit_px": float(c[i])})
            open_pos = None
        if st != 0 and open_pos is None:
            atr = float(d.get("entry_atr") or 0.0)
            if atr > 0:
                open_pos = {"entry_i": i, "entry_px": float(d["entry_px"]),
                            "entry_atr": atr, "dirn": 1 if st == 1 else -1,
                            "dir_state": st, "leg": d.get("entry_leg") or "unknown"}
    return out


def blank():
    return {"n": 0, "filled": 0, "timeout": 0, "cancelled": 0,
            "delta_sum_R": 0.0, "fill_improve_R": [], "mkt_sum_R": 0.0,
            "new_sum_R": 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--out", default="/tmp/execution_entry.json")
    args = ap.parse_args()
    s = get_settings()
    symbols = [x.symbol for x in s.main_contracts]
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
    eng = create_engine(s.db_url, pool_size=4, max_overflow=8)
    sess = sessionmaker(bind=eng)()
    p = fusion_params_from_config()
    contract_meta = {r[0]: (float(r[1] or 1), float(r[2] or 1)) for r in sess.execute(text(
        "SELECT main_symbol, multiplier, tick_size FROM l3_ref.dim_variety"))}

    variants = ["MKT"] + [f"q{q:g}_h{hh}_{mode}" for q in QS for hh in HS for mode in ("fallback", "skip")]
    agg = {tag: {v: blank() for v in variants} for tag in ("ALL", "IS", "OOS")}
    detail = []
    align_n = 0
    align_bad = 0
    coverage_missing = 0

    for si, sym in enumerate(symbols):
        df = _read_hourly(sess, sym, "bar_60m", None, None)
        if df is None or len(df) < 300:
            continue
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        dts = pd.to_datetime(df["dt"])
        trades = trades_from_engine(o, h, l, c, p)
        if not trades:
            continue
        t0 = dts.iloc[min(x["entry_i"] for x in trades)]
        t1 = dts.iloc[max(x["exit_i"] for x in trades)]
        rr = sess.execute(text(
            "SELECT bucket, open, high, low, close FROM l1_mkt.bar_5m "
            "WHERE symbol=:sym AND bucket>=:t0 AND bucket<=:t1 ORDER BY bucket"),
            {"sym": sym, "t0": t0, "t1": t1}).all()
        if not rr:
            coverage_missing += len(trades)
            continue
        m = pd.DataFrame(rr, columns=["dt", "open", "high", "low", "close"])
        mdts = pd.to_datetime(m["dt"])
        mo, mh, ml, mc = (m[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        miv = mdts.values

        for tr in trades:
            ei, xi = tr["entry_i"], tr["exit_i"]
            sig_dt, exit_dt = dts.iloc[ei], dts.iloc[xi]
            dirn = tr["dirn"]
            risk = 2.0 * tr["entry_atr"]
            base_r = dirn * (tr["exit_px"] - tr["entry_px"]) / risk
            sig_pos = int(mdts.searchsorted(sig_dt, side="left"))
            if sig_pos >= len(m) or mdts.iloc[sig_pos] != sig_dt:
                coverage_missing += 1
                continue
            align_n += 1
            if abs(float(mc[sig_pos]) - tr["entry_px"]) > 1e-8:
                align_bad += 1
            # 严格从信号之后的下一根 5m 开始；结束于原离场时刻（含）。
            j0 = sig_pos + 1
            j1 = int(mdts.searchsorted(exit_dt, side="right")) - 1
            if j0 >= len(m) or j1 < j0:
                coverage_missing += 1
                continue
            mkt_px = float(mo[j0])
            mkt_r = dirn * (tr["exit_px"] - mkt_px) / risk
            period = "IS" if sig_dt < IS_END else "OOS"
            rec = {"symbol": sym, "entry_dt": str(sig_dt), "period": period,
                   "side": "LONG" if dirn > 0 else "SHORT", "leg": tr["leg"],
                   "base_R": base_r, "mkt_R": mkt_r,
                   "entry_atr": tr["entry_atr"], "risk_yuan": risk * contract_meta.get(sym, (1, 1))[0],
                   "multiplier": contract_meta.get(sym, (1, 1))[0],
                   "tick_size": contract_meta.get(sym, (1, 1))[1]}

            # MKT 自身的 delta=0，仅记录组合基准。
            for tag in ("ALL", period):
                a = agg[tag]["MKT"]
                a["n"] += 1; a["filled"] += 1
                a["mkt_sum_R"] += mkt_r; a["new_sum_R"] += mkt_r

            for q in QS:
                limit = tr["entry_px"] - dirn * q * tr["entry_atr"]
                for hh in HS:
                    # 先找一次（q,h）的成交/超时状态，两个 mode 共用。
                    fill_pos = None
                    last = min(j0 + hh - 1, j1)
                    for j in range(j0, last + 1):
                        touched = ml[j] <= limit if dirn > 0 else mh[j] >= limit
                        if touched:
                            fill_pos = j
                            break
                    cancelled = fill_pos is None and last == j1 and j1 < j0 + hh - 1
                    timeout = fill_pos is None and not cancelled
                    # 若成交，跳空穿越限价按更优开盘价；否则按限价。
                    fill_px = None
                    if fill_pos is not None:
                        fill_px = (min(float(mo[fill_pos]), limit) if dirn > 0
                                   else max(float(mo[fill_pos]), limit))
                    for mode in ("fallback", "skip"):
                        name = f"q{q:g}_h{hh}_{mode}"
                        is_fill = fill_pos is not None
                        is_fallback = (timeout and mode == "fallback"
                                       and last + 1 <= j1 and last + 1 < len(m))
                        if is_fallback:
                            px = float(mo[last + 1])
                        elif is_fill:
                            px = fill_px
                        else:
                            px = None
                        if px is None:
                            new_r = 0.0
                            delta = -mkt_r
                            imp = None
                        else:
                            new_r = dirn * (tr["exit_px"] - px) / risk
                            delta = new_r - mkt_r
                            imp = dirn * (mkt_px - px) / risk
                        rec[name] = {"delta_R": delta,
                                     "delta_yuan": delta * rec["risk_yuan"],
                                     "filled": px is not None,
                                     "limit_filled": is_fill, "px": px}
                        for tag in ("ALL", period):
                            a = agg[tag][name]
                            a["n"] += 1
                            a["filled"] += int(px is not None)
                            a["timeout"] += int(timeout)
                            a["cancelled"] += int(cancelled)
                            a["delta_sum_R"] += delta
                            a["mkt_sum_R"] += mkt_r
                            a["new_sum_R"] += new_r
                            if imp is not None:
                                a["fill_improve_R"].append(imp)
            detail.append(rec)
        if (si + 1) % 5 == 0:
            print(f"[{si+1}/{len(symbols)}] trades={len(detail)}", flush=True)

    def clean(a):
        fills = a.pop("fill_improve_R")
        a["fill_rate_pct"] = round(100 * a["filled"] / a["n"], 2) if a["n"] else 0
        a["delta_sum_R"] = round(a["delta_sum_R"], 2)
        a["mkt_sum_R"] = round(a["mkt_sum_R"], 2)
        a["new_sum_R"] = round(a["new_sum_R"], 2)
        a["delta_per_trade_R"] = round(a["delta_sum_R"] / a["n"], 5) if a["n"] else 0
        a["fill_improve_med_R"] = round(float(np.median(fills)), 5) if fills else None
        return a

    report = {"rows": len(detail), "alignment": {"checked": align_n,
              "close_mismatch": align_bad, "coverage_missing": coverage_missing},
              "summary": {tag: {v: clean(a) for v, a in vv.items()} for tag, vv in agg.items()}}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    Path(args.out.replace(".json", "_rows.json")).write_text(
        json.dumps(detail, ensure_ascii=False), encoding="utf-8")
    print(f"→ {args.out} rows={len(detail)} align_bad={align_bad} missing={coverage_missing}")


if __name__ == "__main__":
    main()
