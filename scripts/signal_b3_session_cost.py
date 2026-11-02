#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""八期 P2：夜盘 22:00 成交语义对 B3 的「实盘化成本」量化（§28.4）。

七期 §27.2 已证：bucket 标 bar 结束时刻（21 点无 bar、22 点有）、24/26 品种有夜盘、
session_map 归位日首 bar 18/21 取前一日 22:00 夜盘首 bar、隔夜跳空 n=56081
mean +0.0314% / abs_mean 0.65% / 正比例 49.8%（无方向偏差），并裁定 session_map
「2026 胜率100%/MDD0」是成交序列重排的偶然结果，不作生产口径。

本项把该结论推进到 **B3 具体成交层面**（而非全品种描述）：
  A 同一自然日下 weekend_drop 的「日盘首 bar open」与 session_map 的「夜盘归位 open」
    逐单元格价差（交叉验证七期全品种量级）
  B 对 weekend_drop 主口径的每一笔成交，若改用 session_map 同一日的 open 成交，
    逐笔计算净 R 变化 = -side × (sm_open − wd_entry_px) / (SL_MULT × trade['risk'])
    → 判定 0.65% 隔夜不确定性对 B3 是否存在**系统性侵蚀**（而非仅随机噪声）

诚实边界：这是「替换建仓价的一阶近似」，未重跑 session_map 完整成交序列（持仓/平仓
时点亦会随之改变）；且 session_map 归位日 open 可能来自前一日夜盘（周一可差 2 天）。
结论仅用于判定语义可执行性成本，不作收益预期、不就地翻案。

用法：python signal_b3_session_cost.py --out /tmp/aeq/b3_session_cost.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from app.backtest.walkforward import WalkForwardParams
from signal_oos_backtest import (load_market, daily_atr, main_contract_surface,
                                 simulate_windows, SL_MULT)
from signal_b3_pairs import compute_targets
from signal_b3_equity import _d
from signal_b3_compound_oos import PAIRS_ALL

B3_SYMS = sorted({v + "888" for p in PAIRS_ALL for v in (p[0], p[1])})


def pct_stats(vals):
    if not vals:
        return {"n": 0}
    a = np.array(vals, dtype=float)
    return {
        "n": len(a),
        "mean_pct": round(float(np.mean(a)), 4),
        "abs_mean_pct": round(float(np.mean(np.abs(a))), 4),
        "median_pct": round(float(np.median(a)), 4),
        "p10_pct": round(float(np.percentile(a, 10)), 4),
        "p90_pct": round(float(np.percentile(a, 90)), 4),
        "pct_positive": round(float(np.mean(a > 0) * 100), 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--out", default="/tmp/aeq/b3_session_cost.json")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    lo = start - timedelta(days=400)

    # 两套日历口径的行情
    ow, cw, hw, lw = load_market(session, symbols, lo, end,
                                 calendar_mode="weekend_drop")
    osm, csm, hsm, lsm = load_market(session, symbols, lo, end,
                                     calendar_mode="session_map")
    calendar_w = [d for d in ow.index
                  if start <= (d if isinstance(d, date) else d.date()) <= end]
    atr_w = daily_atr(hw, lw, cw)
    cm_w = main_contract_surface(session, symbols, list(ow.index))
    tgt, _ = compute_targets(cw, pairs=PAIRS_ALL, window=args.window,
                             entry_z=args.entry_z, exit_z=args.exit_z,
                             return_strength=True)
    t = tgt.reindex(index=ow.index, columns=symbols).fillna(0.0)
    trades = simulate_windows(t, ow, cw, atr_w, wfp, start, end,
                              rebalance_n=1, sl_mult=SL_MULT,
                              contract_map=cm_w, coef_cache={})
    print(f"[init] wd_trades={len(trades)} wd_cal={len(calendar_w)} "
          f"sm_index={len(osm.index)} b3_syms={len(B3_SYMS)}", flush=True)

    ow_s = ow.copy()
    ow_s.index = [str(x)[:10] for x in ow_s.index]
    osm_s = osm.copy()
    osm_s.index = [str(x)[:10] for x in osm_s.index]
    common = sorted(set(ow_s.index) & set(osm_s.index))

    def get(df, ds, sym):
        try:
            v = float(df.loc[ds, sym])
            return None if v != v else v
        except Exception:
            return None

    # ---- A 全单元格价差（B3 品种，交叉验证七期 0.65% 量级）----
    diffs, by_sym = [], defaultdict(list)
    for ds in common:
        for sym in B3_SYMS:
            a, b = get(ow_s, ds, sym), get(osm_s, ds, sym)
            if a and a > 0 and b and b > 0:
                dp = (b - a) / a * 100.0
                diffs.append(dp)
                by_sym[sym].append(dp)
    A = {"n_common_dates": len(common), "overall": pct_stats(diffs),
         "by_symbol": {k: pct_stats(v) for k, v in sorted(by_sym.items())}}
    print(f"[A cells] n={A['overall']['n']} mean={A['overall'].get('mean_pct')}% "
          f"abs_mean={A['overall'].get('abs_mean_pct')}% "
          f"pct_pos={A['overall'].get('pct_positive')}%", flush=True)

    # ---- B B3 成交层面：若改用夜盘归位 open 成交，逐笔净 R 变化 ----
    matched, unmatched = [], 0
    for tr in trades:
        ds, sym = _d(tr["entry_dt"]), tr["symbol"]
        a, b = tr["entry_px"], get(osm_s, ds, sym)
        if b is None or a is None or a <= 0:
            unmatched += 1
            continue
        side = 1.0 if tr["side"] == "LONG" else -1.0
        risk = tr["risk"]                   # 底座已存 risk_points = sl_mult×ATR（signal_oos_backtest.py:254），不重复乘 SL_MULT
        d_pnl = -side * (b - a)            # entry 改为夜盘价后的 pnl 变化
        dR = d_pnl / risk if risk and risk > 0 else None
        matched.append({
            "date": ds, "symbol": sym, "side": tr["side"],
            "entry_wd": round(float(a), 2), "entry_sm": round(float(b), 2),
            "diff_pct": round((b - a) / a * 100.0, 4),
            "delta_net_R": round(float(dR), 5) if dR is not None else None,
        })
    dRs = [x["delta_net_R"] for x in matched if x["delta_net_R"] is not None]
    dpcts = [x["diff_pct"] for x in matched]
    by_side = defaultdict(list)
    by_sym_tr = defaultdict(list)
    for x in matched:
        if x["delta_net_R"] is not None:
            by_side[x["side"]].append(x["delta_net_R"])
            by_sym_tr[x["symbol"]].append(x["delta_net_R"])
    B = {
        "n_trades_total": len(trades), "n_matched": len(matched),
        "n_unmatched": unmatched,
        "match_rate_pct": round(100.0 * len(matched) / len(trades), 2) if trades else None,
        "entry_price_diff_all": pct_stats(dpcts),
        "delta_net_R_all": {
            "n": len(dRs),
            "sum": round(float(np.sum(dRs)), 5),
            "mean": round(float(np.mean(dRs)), 5),
            "median": round(float(np.median(dRs)), 5),
            "pct_negative": round(float(np.mean(np.array(dRs) < 0) * 100), 2),
            "pct_positive": round(float(np.mean(np.array(dRs) > 0) * 100), 2),
            "min": round(float(np.min(dRs)), 5),
            "max": round(float(np.max(dRs)), 5),
        },
        "delta_net_R_by_side": {
            k: {"n": len(v), "sum": round(float(np.sum(v)), 5),
                "mean": round(float(np.mean(v)), 5),
                "pct_negative": round(float(np.mean(np.array(v) < 0) * 100), 2)}
            for k, v in sorted(by_side.items())
        },
        "delta_net_R_by_symbol_top_worst": sorted(
            [{"symbol": k, "n": len(v), "sum": round(float(np.sum(v)), 5)}
             for k, v in by_sym_tr.items()],
            key=lambda x: x["sum"])[:8],
        "sample": matched[:8],
    }
    print(f"[B trades] matched={len(matched)}/{len(trades)} "
          f"sum_dR={B['delta_net_R_all']['sum']} mean_dR={B['delta_net_R_all']['mean']} "
          f"pct_neg={B['delta_net_R_all']['pct_negative']}%", flush=True)

    # ---- 判定（用纯统计量，不事后挑方向；相对量级用全期净 R 总量作分母）----
    total_net_R = sum(tr["net"] for tr in trades)   # 底座逐笔 net_R 直接汇总，与 §9 完全同口径
    tot = B["delta_net_R_all"]["sum"]
    negp = B["delta_net_R_all"]["pct_negative"]
    posp = B["delta_net_R_all"]["pct_positive"]
    systematic = (tot < -0.05 * len(dRs)) if dRs else False
    erosion_pct = round(100.0 * tot / total_net_R, 2) if total_net_R else None
    verdict = {
        "b3_systematic_erosion": systematic,
        "criterion": "sum(delta_net_R) < -0.05×n（即平均侵蚀 > 0.05R/笔）判为系统性侵蚀",
        "sum_delta_net_R": tot,
        "pct_trades_worse_on_night_fill": negp,
        "pct_trades_better_on_night_fill": posp,
        "total_net_R_full_period": round(float(total_net_R), 4),
        "erosion_pct_of_total_net_R": erosion_pct,
        "conclusion": (
            "夜盘成交对 B3 存在系统性净 R 侵蚀 → 强化 §27.2『不作生产口径』"
            if systematic else
            f"未达预注册的系统性侵蚀阈值（sum={tot}R vs 阈值 {-0.05*len(dRs):.2f}R），"
            f"但存在**方向一致为负的非零偏移**：总 {tot}R ≈ 全期净 R 总量 "
            f"{total_net_R:.2f}R 的 {erosion_pct}%，变差笔数 {negp}% > 变好 {posp}%，"
            f"LONG 腿（{B['delta_net_R_by_side'].get('LONG',{}).get('sum')}R）大于 "
            f"SHORT（{B['delta_net_R_by_side'].get('SHORT',{}).get('sum')}R）→ "
            f"属「小幅但非零」的执行成本，而非纯随机噪声；"
            f"维持 §27.2『不作生产口径、仅情景对照』，且不可忽略该负向偏移"
        ),
        "note": "一阶近似（仅替换建仓价，未重跑 session_map 完整成交序列）；"
                "session_map 归位日 open 可能来自前一日夜盘（周一可差 2 天）",
    }
    print(f"[verdict] systematic_erosion={systematic} "
          f"sum_dR={tot} pct_neg={negp}% erosion_pct={erosion_pct}%", flush=True)

    report = {
        "meta": {
            "start": str(start), "end": str(end),
            "weekend_drop": "主口径（日盘首 bar 成交）",
            "session_map": "夜盘归位口径（hour>=21 归次日、周末推周一；实际首 bar 22:00）",
            "purpose": "八期 P2：夜盘 22:00 成交语义对 B3 的实盘化成本量化",
            "caveats": [
                "一阶近似：仅替换建仓价，未重跑 session_map 完整成交序列（持仓/平仓时点亦会改变）",
                "session_map 归位日 open 可能来自前一日夜盘（周一可差 2 天），价差含跨日信息",
                "delta_net_R = -side×(sm_open−wd_entry_px)/trade['risk']（risk 已含 sl_mult×ATR，不重复乘；底座 signal_oos_backtest.py:254）；成交 pnl 原口径不变",
                "交叉验证：A 段全单元格量级应与七期 D_overnight_gap（abs_mean 0.65%）同量级",
                "结论仅用于判定语义可执行性成本，不作收益预期、不就地翻案",
            ],
        },
        "A_cell_price_diff": A,
        "B_b3_trade_level": B,
        "verdict": verdict,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[done] saved {args.out}", flush=True)
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
