# -*- coding: utf-8 -*-
"""金额口径核对（部署前后一致性 + 盈亏全貌）。

为什么单独写一份：``authority_backtest.py`` 只给 R 口径（毛 edge +0.0166R vs
成本 0.0489R），用户真正要看的是**元**。而元口径此前只在 walk-forward
（``validate_exit_candidate.py``）里出现过（−245,799 元），那是分段滚动窗口，
与"全样本一次性回测"不是同一个数，两者混用会误判。

口径与 payout 定义（严格对齐引擎）：
    毛元/手   = (pnl + cost_points) * mult        # 未扣成本的价差
    成本元/手 = cost_yuan = cost_points * mult    # 引擎按建仓日历史费率算
    净元/手   = pnl * mult                        # 毛 − 成本
每笔按 1 手计（add_max_lots 只是加仓上限，引擎按手逐笔出 trade）。

跑法（生产机，systemd 同款环境）：
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc \
        /home/ubuntu/qhyc_work/venv/bin/python /tmp/pnl_amount_check.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import numpy as np

from app.core.db import session_scope
from app.backtest.fusion_backtest import (
    fusion_params_from_config,
    run_fusion_backtest,
)


def money(x: float) -> str:
    return f"{x:+,.0f}"


def block(trades: list[dict], name: str) -> dict:
    if not trades:
        print(f"  {name}: 无样本")
        return {}
    mult = np.array([float(t["mult"]) for t in trades], float)
    pnl = np.array([float(t["pnl"]) for t in trades], float)
    cpt = np.array([float(t["cost_points"]) for t in trades], float)
    gross_y = (pnl + cpt) * mult
    cost_y = cpt * mult
    net_y = pnl * mult
    win = net_y > 0
    print(f"  {name}: N={len(trades)}  毛={money(gross_y.sum())} 元  "
          f"成本={money(cost_y.sum())} 元  净={money(net_y.sum())} 元  "
          f"胜率={win.mean()*100:.1f}%  成本/毛={cost_y.sum()/gross_y.sum()*100:.1f}%"
          if gross_y.sum() != 0 else f"  {name}: 毛=0")
    return {
        "n": len(trades), "gross_yuan": float(gross_y.sum()),
        "cost_yuan": float(cost_y.sum()), "net_yuan": float(net_y.sum()),
        "win_rate": float(win.mean()),
        "cost_over_gross_pct": float(cost_y.sum() / gross_y.sum() * 100)
        if gross_y.sum() else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--split", default="2024-01-01", help="IS/OOS 分界")
    args = ap.parse_args()

    params = fusion_params_from_config()
    print("=== 生产口径参数 ===")
    print(f"  ema_k={params.ema_k} sl_atr={params.sl_atr} trail_atr={params.trail_atr} "
          f"be_r={params.be_r} adx_min={params.adx_min} src={params.src}")

    with session_scope() as sess:
        res = run_fusion_backtest(
            sess, symbols=None, params=params,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            include_trades=True,
        )

    trades = [t for t in res["trades"] if not t.get("open")]
    print(f"\n=== 全样本 {args.start}~{args.end}（品种={len(res['per_symbol'])}，未平仓={len(res['trades'])-len(trades)}）===")
    total = block(trades, "全样本")

    # ---- 是否有 symbol 字段（引擎 _trade 里写死 None，需靠 per_symbol 兜底）----
    has_sym = any(t.get("symbol") for t in trades)

    # ---- IS / OOS ----
    sp = date.fromisoformat(args.split)

    def _d(t):
        d = t["entry_dt"]
        return d.date() if hasattr(d, "date") else d

    is_tr = [t for t in trades if _d(t) < sp]
    oos_tr = [t for t in trades if _d(t) >= sp]
    print(f"\n=== IS/OOS 分界 {args.split} ===")
    is_blk = block(is_tr, f"IS  <{args.split}")
    oos_blk = block(oos_tr, f"OOS >={args.split}")

    # ---- 逐年 ----
    print("\n=== 逐年净盈亏（元，按建仓年）===")
    years = sorted({_d(t).year for t in trades})
    per_year = {}
    for y in years:
        ys = [t for t in trades if _d(t).year == y]
        b = block(ys, f"{y}")
        per_year[str(y)] = b

    # ---- 逐品种（若有字段）----
    per_sym: dict[str, dict] = {}
    if has_sym:
        print("\n=== 逐品种净盈亏（Top/Bottom 8）===")
        for s in sorted({t["symbol"] for t in trades}):
            ss = [t for t in trades if t["symbol"] == s]
            per_sym[s] = block(ss, s)
        ranked = sorted(per_sym.items(), key=lambda kv: kv[1].get("net_yuan", 0))
        print("\n  —— 最亏 8：")
        for k, v in ranked[:8]:
            print(f"     {k:<8} 净={money(v['net_yuan'])} 元  N={v['n']}")
        print("  —— 最赚 8：")
        for k, v in ranked[-8:][::-1]:
            print(f"     {k:<8} 净={money(v['net_yuan'])} 元  N={v['n']}")
    else:
        print("\n（trade 无 symbol 字段，逐品种金额略过；如需请用 per_symbol 分品种重跑）")

    print("\n=== 一句话 ===")
    if total:
        print(f"  全样本净盈亏 = {money(total['net_yuan'])} 元 "
              f"（毛 {money(total['gross_yuan'])} − 成本 {money(total['cost_yuan'])}，"
              f"成本占毛 {total['cost_over_gross_pct']:.1f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
