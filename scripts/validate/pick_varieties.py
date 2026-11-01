# -*- coding: utf-8 -*-
"""逐品种成本核算选型：算清每个品种的「毛 edge − 真实成本 = 净 edge」。

背景：三次独立回测（2026-10-03 诊断、今日权威 R 口径、今日 walk-forward 金额口径）
一致证明亏损来自「结构」：信号太频繁 × 每笔都要付真实成本（成本占毛利 130.3%）。
调离场参数无效（SL1.0/TR6.0 候选 OOS 更差）。有证据支撑的方向只剩三条：
降频 / 降成本 / 只做强品种。本脚本服务第三条——用真实费率把品种分档。

方法：生产口径全品种回测一次（trade 自带 symbol），逐品种分组统计：
  gross_R  = 成交 R 序列均值（未扣成本）
  cost_R   = median(cost_yuan / (risk*mult))  真实费率折算的 R 成本
  net_R    = gross_R - cost_R                 净 edge（本脚本的核心指标）
  t_stat   = gross 的 t 值，用于剔除「样本太少偶然为正」
判据：本脚本只做截面排序；品种入选前须另跑一段区间验同号（防止单样本偶然为正）。

用法（云端容器内）：
  PYTHONPATH=/app python scripts/validate/pick_varieties.py --o /tmp/variety_pick.json
"""
from __future__ import annotations

import argparse
import json
from datetime import date

import numpy as np

from app.core.db import session_scope
from app.backtest.fusion_backtest import run_fusion_backtest, fusion_params_from_config


def p(*a):
    print(*a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--o", default="/tmp/variety_pick.json")
    ap.add_argument("--min-trades", type=int, default=30,
                    help="品种最少成交笔数，低于此值判样本不足不参与排名")
    args = ap.parse_args()

    params = fusion_params_from_config()
    p("=== 逐品种净 edge 选型（生产口径，真实费率）===")
    p(f"区间 {args.start} ~ {args.end}  src={params.src}")

    with session_scope() as sess:
        res = run_fusion_backtest(
            sess, symbols=None, params=params,
            start=date.fromisoformat(args.start), end=date.fromisoformat(args.end),
            include_trades=True,
        )
    closed = [t for t in res["trades"] if not t.get("open")]
    p(f"已平仓成交 = {len(closed)}   品种数 = {len(res['per_symbol'])}"
      f"   跳过 = {len(res.get('skipped') or [])}")

    # ---- 按 symbol 分组，逐品种算 net edge ----
    by_sym: dict[str, list] = {}
    for t in closed:
        by_sym.setdefault(t.get("symbol"), []).append(t)

    rows = []
    for sym, ts in sorted(by_sym.items()):
        if len(ts) < args.min_trades:
            continue
        R = np.asarray([t["R"] for t in ts], float)
        cost = np.asarray([t.get("cost_yuan") or 0.0 for t in ts], float)
        risk = np.asarray([(t.get("risk") or 0.0) * (t.get("mult") or 0.0) for t in ts], float)
        ok = risk > 0
        if int(ok.sum()) < args.min_trades:
            continue
        cost_R = float(np.median(cost[ok] / risk[ok]))
        gross = float(R.mean())
        net = gross - cost_R
        w = R > 0
        pf = float(R[w].sum() / abs(R[~w].sum())) if (~w).any() and R[~w].sum() != 0 else float("inf")
        sd = float(R.std(ddof=1)) if len(R) > 1 else 0.0
        tstat = gross / (sd / np.sqrt(len(R))) if sd > 0 else 0.0
        rows.append({
            "symbol": sym, "n": len(ts), "gross_R": round(gross, 4),
            "cost_R": round(cost_R, 4), "net_R": round(net, 4),
            "win_rate": round(float(w.mean()), 4), "PF": round(pf, 3),
            "t_stat": round(tstat, 2), "total_R": round(float(R.sum()), 1),
        })

    rows.sort(key=lambda r: r["net_R"], reverse=True)

    p(f"\n=== 逐品种排名（样本 >= {args.min_trades} 笔，共 {len(rows)} 品种）===")
    p(f"{'symbol':<10}{'笔数':>7}{'毛edge':>9}{'成本R':>9}{'净edge':>9}{'胜率':>8}{'PF':>7}{'t值':>7}")
    for r in rows:
        p(f"{r['symbol']:<10}{r['n']:>7}{r['gross_R']:>+9.4f}{r['cost_R']:>9.4f}"
          f"{r['net_R']:>+9.4f}{r['win_rate'] * 100:>7.1f}%{r['PF']:>7.2f}{r['t_stat']:>7.2f}")

    # ---- 三档划分：净 edge 为正还不够，必须 t 与 PF 站得住 ----
    pos = [r for r in rows if r["net_R"] > 0]
    neg = [r for r in rows if r["net_R"] <= 0]
    strong = [r for r in pos if abs(r["t_stat"]) > 2 and r["PF"] > 1.2]
    weak = [r for r in pos if not (abs(r["t_stat"]) > 2 and r["PF"] > 1.2)]

    p("\n=== 分档结论 ===")
    p(f"  [A档 建议保留] 净edge>0 且 t>2 且 PF>1.2  n={len(strong)}")
    if strong:
        p("    " + ", ".join(f"{r['symbol']}(净{r['net_R']:+.4f}/PF{r['PF']:.2f}/t{r['t_stat']:.1f})"
                             for r in strong))
    p(f"  [B档 观察/轻仓] 净edge>0 但显著性不足  n={len(weak)}")
    if weak:
        p("    " + ", ".join(f"{r['symbol']}(净{r['net_R']:+.4f}/PF{r['PF']:.2f}/t{r['t_stat']:.1f})"
                             for r in weak))
    p(f"  [C档 建议剔除] 净edge<=0  n={len(neg)}")
    if neg:
        p("    " + ", ".join(f"{r['symbol']}(净{r['net_R']:+.4f})" for r in neg))

    with open(args.o, "w", encoding="utf-8") as f:
        json.dump({"params": res["params"], "range": [args.start, args.end],
                   "min_trades": args.min_trades, "rows": rows,
                   "grade_A": [r["symbol"] for r in strong],
                   "grade_B": [r["symbol"] for r in weak],
                   "grade_C": [r["symbol"] for r in neg]},
                  f, ensure_ascii=False, indent=2, default=str)
    p(f"\n结果已写 {args.o}")


if __name__ == "__main__":
    main()
