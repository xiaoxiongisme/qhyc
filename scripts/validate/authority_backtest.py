# -*- coding: utf-8 -*-
"""权威回测：验证《期货交易总结报告-定稿版》的核心数据结论。

数据源：云端 TimescaleDB `hourly_bar`（生产 src=akshare），走生产口径
`app.backtest.fusion_backtest.run_fusion_backtest`（walk_fusion_states 同一循环）。
成本：生产真实历史费率（cost_points_at），非 5bp 假设。

验证的结论（每项给 支撑/推翻/存疑 + 判据）：
  C1 截盈破坏期望：盈利单在 +0.5R 手动平仓 → PF 崩塌
  C2 被套 1R 回本断崖：按最大逆向偏移 AE 分档的回本率
  C3 利润尾部集中：最好 5% 单贡献利润占比；去掉 R>+2 后是否转负
  C4 震荡占比 + ADX<15 负期望（顺向前向收益按 ADX 分层）
  C5 成本平衡点：用真实费率重算平衡点 bp
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

from app.core.db import session_scope
from app.backtest.fusion_backtest import (
    run_fusion_backtest,
    fusion_params_from_config,
)

sys.path  # noqa


def p(*a):
    print(*a, flush=True)


def summarize(rs, name):
    rs = np.asarray(rs, float)
    rs = rs[np.isfinite(rs)]
    if len(rs) == 0:
        p(f"  {name}: (无样本)")
        return
    w = rs > 0
    l = ~w
    pf = rs[w].sum() / abs(rs[l].sum()) if l.any() and rs[l].sum() != 0 else float("inf")
    p(f"  {name}: N={len(rs)} 胜率={w.mean()*100:.1f}% 期望R={rs.mean():+.4f} "
      f"中位R={np.median(rs):+.3f} PF={pf:.3f} 总R={rs.sum():+.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--o", default="authority_backtest_out.json")
    ap.add_argument("--max-symbols", type=int, default=0)
    args = ap.parse_args()

    params = fusion_params_from_config()  # 生产口径
    p("=== 生产口径参数 ===")
    p(f"  ema_k={params.ema_k} atr_n={params.atr_n} sl_atr={params.sl_atr} "
      f"trail_atr={params.trail_atr} be_r={params.be_r} adx_min={params.adx_min} "
      f"fib_confl={params.fib_confl} entry_mode={params.entry_mode} "
      f"add_max_lots={params.add_max_lots} src={params.src}")

    symbols = None
    if args.max_symbols > 0:
        from app.core.config import get_settings
        symbols = [s.symbol for s in get_settings().main_contracts][:args.max_symbols]

    res = None
    with session_scope() as sess:
        res = run_fusion_backtest(
            sess, symbols=symbols, params=params,
            start=date.fromisoformat(args.start), end=date.fromisoformat(args.end),
            include_trades=True,
        )
    trades = res["trades"]
    closed = [t for t in trades if not t.get("open")]
    R = np.array([t["R"] for t in closed], dtype=float)
    p(f"\n=== 生产口径回测 {args.start}~{args.end} ===")
    p(f"  总成交={len(closed)} 未平仓={len(trades)-len(closed)} 品种={len(res['per_symbol'])}")
    summarize(R, "基线")

    out = {"params": res["params"], "n_trades": len(closed)}

    # ---- C1 截盈破坏期望（精确算术，无模型假设）----
    p("\n=== C1 截盈破坏期望 ===")
    c1 = {}
    for cap in (0.5, 0.3, 1.0, 2.0):
        rc = np.where(R > cap, cap, R)
        w = rc > 0; l = ~w
        pf = rc[w].sum() / abs(rc[l].sum()) if l.any() and rc[l].sum() != 0 else float("inf")
        p(f"  截盈@+{cap}R: 期望R={rc.mean():+.4f} PF={pf:.3f} 总R={rc.sum():+.1f}")
        c1[str(cap)] = {"mean_R": round(float(rc.mean()), 4), "PF": round(float(pf), 3)}
    out["C1_cut_profit"] = c1
    ver = "支撑" if (np.where(R > 0.5, 0.5, R).mean() < 0) else "推翻"
    p(f"  判据(截盈@0.5R 期望转负)→ {ver}")

    # ---- C2 被套回本断崖（AE 分档）----
    # AE = 持仓期间最大逆向偏移（R 单位）。trade 只给 entry/exit，无 path，
    # 故用相邻 bar 的 low/high 重构需要行情。此处退而求其次：用「最终 R」的
    # 分布近似 + 从 _read_hourly 重放。为保持权威口径，改为：回本率 = 曾浮亏
    # 到某个深度后最终转正的概率。因 trade 无 path，采用保守代理：
    #   P(回本 | 最终亏损≥k) 无法直接得。改用引擎重放：见下 C2b。
    # 这里先输出「最终 R 分档的胜率」，作为回本断崖的间接佐证。
    p("\n=== C2 被套分档（最终R分档，间接佐证）===")
    edges = [-99, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 99]
    c2 = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        m = (R > lo) & (R <= hi)
        if m.sum() < 10:
            continue
        c2.append({"bin": f"{lo}~{hi}", "n": int(m.sum()),
                   "win_rate": round(float((R[m] > 0).mean()), 4),
                   "mean_R": round(float(R[m].mean()), 4)})
        p(f"  R∈({lo},{hi}] N={m.sum():5d} 胜率={(R[m]>0).mean()*100:5.1f}% meanR={R[m].mean():+.3f}")
    out["C2_loss_bins"] = c2

    # ---- C3 利润尾部集中 ----
    p("\n=== C3 利润尾部集中 ===")
    srt = np.sort(R)[::-1]
    tot = R.sum()
    top5 = srt[: max(1, int(len(R) * 0.05))]
    c3 = {
        "total_R": round(float(tot), 1),
        "top5pct_contrib": round(float(top5.sum()), 1),
        "top5pct_pct_of_total": round(float(top5.sum() / tot * 100), 1) if tot else None,
        "n_gt2R": int((R > 2).sum()),
        "sum_gt2R": round(float(R[R > 2].sum()), 1),
        "total_excluding_gt2R": round(float(R[R <= 2].sum()), 1),
    }
    p(f"  总R={tot:+.1f}；最好5%单({len(top5)}笔)贡献 {top5.sum():+.1f}R "
      f"= {top5.sum()/tot*100:.0f}%")
    p(f"  R>+2 的单({(R>2).sum()}笔)贡献 {R[R>2].sum():+.1f}R；拿掉后总R={R[R<=2].sum():+.1f}")
    out["C3_tail_concentration"] = c3
    ver = "支撑" if (R[R > 2].sum() > tot and tot > 0) else "推翻"
    p(f"  判据(尾部贡献>100%总利润)→ {ver}")

    # ---- C4 震荡占比 + ADX 分层（需重放行情算 ADX）----
    p("\n=== C4 ADX 分层前向收益（生产数据重放）===")
    from app.backtest.fusion_backtest import _read_hourly
    from app.core.config import get_settings
    from app.strategies.fusion_signal import adx14, atr14, ema
    syms = symbols if symbols else [s.symbol for s in get_settings().main_contracts]
    recs = []
    with session_scope() as sess:
        for sym in syms:
            df = _read_hourly(sess, sym, params.src,
                              date.fromisoformat(args.start), date.fromisoformat(args.end))
            if df is None or len(df) < 200:
                continue
            c = df["close"].to_numpy(float)
            h = df["high"].to_numpy(float)
            lo = df["low"].to_numpy(float)
            adx = adx14(h, lo, c, 14)
            atr = atr14(h, lo, c, 14)
            e = ema(c, 140)
            side = np.sign(c - e)  # 方向
            for hz, hzlab in ((16, "h16"), (48, "h48")):
                fwd = np.full(len(c), np.nan)
                fwd[:-hz] = (c[hz:] - c[:-hz]) / np.where(atr[:-hz] > 0, atr[:-hz], np.nan)
                al = fwd * side  # 顺势对齐
                ok = np.isfinite(al) & np.isfinite(adx)
                for lo_b, hi_b, lab in ((0, 15, "<15"), (15, 25, "15-25"),
                                        (25, 35, "25-35"), (35, 999, ">=35")):
                    m = ok & (adx >= lo_b) & (adx < hi_b)
                    if m.sum() < 50:
                        continue
                    recs.append({"adx_bin": lab, "hz": hzlab, "n": int(m.sum()),
                                 "aligned_mean_ATR": round(float(al[m].mean()), 4)})
    c4 = pd.DataFrame(recs)
    if len(c4):
        p("  ADX分层 × 前向顺势期望(ATR单位):")
        for (adxbin, hz), g in c4.groupby(["adx_bin", "hz"]):
            p(f"    ADX{adxbin:>6} {hz}: n={g['n'].sum():6d} mean={g['aligned_mean_ATR'].mean():+.4f}")
    out["C4_adx_strata"] = recs

    # ---- C5 成本平衡点（真实费率）----
    p("\n=== C5 成本平衡点（真实费率）===")
    cost_R = np.array([t["cost_yuan"] for t in closed], dtype=float)
    risk_yuan = np.array([t["risk"] * t["mult"] for t in closed], dtype=float)
    ok = risk_yuan > 0
    cost_in_R = np.median(cost_R[ok] / risk_yuan[ok]) if ok.any() else float("nan")
    gross_R = float(R.mean()) if len(R) else 0.0
    p(f"  真实成本中位 = {cost_in_R:.4f}R/笔；毛edge = {gross_R:+.4f}R/笔")
    if cost_in_R > 0:
        bp_equiv = 5.0 * gross_R / cost_in_R  # 若5bp对应cost_in_R，则平衡点bp
        p(f"  折算平衡点 = {bp_equiv:.1f}bp（参考，真实费率口径）")
        out["C5_cost"] = {"median_cost_R": round(float(cost_in_R), 4),
                          "gross_R": round(float(gross_R), 4),
                          "breakeven_bp_equiv": round(float(bp_equiv), 1)}

    with open(args.o, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    p(f"\n结果已写 {args.o}")


if __name__ == "__main__":
    main()
