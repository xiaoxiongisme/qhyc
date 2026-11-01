#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Alpha 层入场质量分析（2026-11-01，研究分支 cb/v2-oos-investigation，未碰 main/cb/dev）。

目的（承接 docs/V2样本外崩塌排查.md §8.5 #2「重新审视入场质量 / 信号逻辑」）：
  V2 崩塌根因是「每笔毛 alpha < 交易成本」（gross<cost，胜率 ~28%，毛 R 微正）。
  本脚本把 OOS 成交拆到 alpha 层，定位**边到底败在哪一环**：

  ① 入场分支（回踩 pb / 突破 bk）× 多空：胜率 / 毛R / 净R / 笔数 —— 哪类入场更差；
  ② 离场原因（初始止损 sl / 吊灯 trail / 保本 be / 强制 force）：分布 + 胜率 ——
     多少是被「一进场就被初始止损打掉」（假信号的直接证据）；
  ③ 持有期分布（bars 与近似天数）：按盈亏分桶 —— 短持有+亏损 = 假信号；
  ④ R 多倍分布：胜率 / 均值毛R / 均值净R / 期望值 / 盈亏比（PF）；
  ⑤ 成本分解（核心）：逐笔 毛R / 成本R / 净R，聚合毛R vs 成本R，
     以及「毛R ≤ 成本R 的成交占比」—— gross<cost 在逐笔层面的证据；
  ⑥ 假信号率：亏损且 |净R|<1（≈被止损在 1R 内）的占比；持仓 ≤3 根即止损的占比；
  ⑦ 加码贡献：首仓 vs 加码手 的胜率 / 净R。

口径（单一真源，与 doc §8 完全一致）：
  - 信号层 walk_fusion_states + 组合层 _simulate_trades（真实乘数 / 真实历史费率成本）；
  - OOS 成交取自 walk-forward 测试窗（train720/test60/step60，reestimate=False），
    仅保留 entry_dt >= te_start 的**已平仓**成交 —— 与 doc §8 的 OOS 定义同口径；
  - 入场/离场原因来自 walk_fusion_states 新增的 entry_reason/exit_reason 元数据字段
    （纯行为中性扩展，不改 state/lots/pnl）。

用法（qhyc-api 容器内，PYTHONPATH 指向研究分支 app）：
  python scripts/alpha_entry_quality.py [--max-symbols N] [--start 2021-01-01] \
      [--end 2026-10-30] [--out /tmp/alpha_eq.json]
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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import (
    fusion_params_from_config, run_fusion_backtest,
)
from app.backtest.walkforward import _windows, _dt

BARS_PER_DAY = 6.7   # 60m 粒度：商品 ~6.7 根/交易日（与 doc §8.2「60m 密度 6.7× 日线」一致）


# ------------------------------------------------------------------- 工具
def gross_r(t):
    return float(t["R"]) if t.get("risk", 0) and t.get("risk") > 0 else None


def cost_r(t):
    return (float(t["cost_points"]) / float(t["risk"])) if t.get("risk", 0) > 0 else None


def net_r(t):
    return (float(t["pnl"]) / float(t["risk"])) if t.get("risk", 0) > 0 else None


def holding_days(t):
    return float(t.get("holding_bars", 0)) / BARS_PER_DAY


def seg_stats(rows: list[dict]) -> dict:
    """rows: 已筛选的 closed 成交列表（每条带 R/netR/costR/holding/branch/side/reason）。"""
    if not rows:
        return {"n": 0}
    g = np.array([r["gross"] for r in rows], dtype=float)
    c = np.array([r["cost"] for r in rows], dtype=float)
    n = np.array([r["net"] for r in rows], dtype=float)
    wins = n > 0
    losses = n < 0
    sum_win = float(n[wins].sum()) if wins.any() else 0.0
    sum_loss = float(n[losses].sum()) if losses.any() else 0.0
    pf = (sum_win / abs(sum_loss)) if sum_loss != 0 else (float("inf") if sum_win > 0 else 0.0)
    gross_le_cost = float((g <= c).mean()) if len(rows) else 0.0  # 毛R不覆盖成本的占比
    return {
        "n": int(len(rows)),
        "win_rate": round(float(wins.mean()) * 100, 2),
        "mean_gross_R": round(float(g.mean()), 5),
        "mean_cost_R": round(float(c.mean()), 5),
        "mean_net_R": round(float(n.mean()), 5),
        "sum_gross_R": round(float(g.sum()), 1),
        "sum_cost_R": round(float(c.sum()), 1),
        "sum_net_R": round(float(n.sum()), 1),
        "expectancy_R": round(float(n.mean()), 5),
        "profit_factor": (round(pf, 3) if pf != float("inf") else "inf"),
        "gross_le_cost_pct": round(gross_le_cost * 100, 2),
    }


def branch_of(t):
    """入场分支标签（仅首仓有意义）。"""
    if t.get("addon"):
        return "addon"
    return t.get("entry_reason") or "na"


def enrich(trades: list[dict]) -> list[dict]:
    out = []
    for t in trades:
        if t.get("open"):
            continue
        g, c, n = gross_r(t), cost_r(t), net_r(t)
        if g is None or c is None or n is None:
            continue
        out.append({
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "entry_reason": t.get("entry_reason"),
            "exit_reason": t.get("exit_reason"),
            "addon": bool(t.get("addon")),
            "holding_bars": int(t.get("holding_bars", 0) or 0),
            "holding_days": holding_days(t),
            "gross": g, "cost": c, "net": n,
            "entry_dt": _dt(t.get("entry_dt")),
            "pnl_pct": float(t.get("pnl_pct", 0.0) or 0.0),
        })
    return out


# ------------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--out", default="/tmp/alpha_eq.json")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts] or []
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    p = fusion_params_from_config()

    # ---- WF-OOS 测试窗循环（与 walkforward.run_walk_forward 同口径，只取 OOS 成交）----
    from app.backtest.walkforward import WalkForwardParams
    wfp = WalkForwardParams(train_days=720, test_days=60, step_days=60,
                            warmup_days=150, reestimate=False)
    all_oos: list[dict] = []
    n_windows = 0
    for tr_s, tr_e, te_s, te_e in _windows(start, end, wfp):
        run_from = te_s - timedelta(days=wfp.warmup_days)
        res = run_fusion_backtest(session, symbols, p, run_from, te_e,
                                  include_trades=True)
        trades = res.get("trades") or []
        for t in trades:
            d = _dt(t.get("entry_dt"))
            if d is None:
                continue
            if d.date() >= te_s and not t.get("open"):
                t["symbol"] = t.get("symbol") or ""
                all_oos.append(t)
        n_windows += 1
    session.close()
    engine.dispose()

    rows = enrich(all_oos)
    print(f"WF-OOS 窗口 {n_windows} 个；OOS 已平仓成交（含成本/R）{len(rows)} 笔")

    # ---- 拆分：早期 OOS（entry<2024-01-01） vs 近期 OOS（>=2024-01-01，doc §8.2 严格 OOS）----
    CUT = date(2024, 1, 1)
    recent = [r for r in rows if r["entry_dt"] and r["entry_dt"].date() >= CUT]
    early = [r for r in rows if r["entry_dt"] and r["entry_dt"].date() < CUT]

    report: dict = {}
    report["meta"] = {
        "symbols": len(symbols), "n_windows": n_windows,
        "start": args.start, "end": args.end, "recent_cut": "2024-01-01",
        "bars_per_day": BARS_PER_DAY,
    }
    report["overall"] = seg_stats(rows)
    report["early_oos(<2024)"] = seg_stats(early)
    report["recent_oos(>=2024)"] = seg_stats(recent)

    # 分年度
    by_year: dict = defaultdict(list)
    for r in rows:
        by_year[r["entry_dt"].year].append(r)
    report["by_year"] = {str(y): seg_stats(v) for y, v in sorted(by_year.items())}

    # ① 入场分支 × 多空（仅首仓）
    def grp(keyfn, pool):
        d: dict = {}
        for r in pool:
            d.setdefault(keyfn(r), []).append(r)
        return {k: seg_stats(v) for k, v in sorted(d.items(), key=lambda x: str(x[0]))}

    first = [r for r in rows if not r["addon"]]
    report["by_entry_branch"] = grp(branch_of, first)
    report["by_side"] = grp(lambda r: r["side"], rows)
    report["by_side_first"] = grp(lambda r: r["side"], first)

    # ② 离场原因
    report["by_exit_reason"] = grp(lambda r: r["exit_reason"], rows)
    report["recent_by_exit_reason"] = grp(lambda r: r["exit_reason"], recent)

    # ③ 持有期分桶（仅首仓，避免加码短持有干扰）
    def hold_bin(r):
        b = r["holding_bars"]
        if b <= 3:
            return "00_<=3bars"
        if b <= 10:
            return "01_4-10"
        if b <= 30:
            return "02_11-30"
        if b <= 100:
            return "03_31-100"
        return "04_>100"
    report["by_holding_bin"] = grp(hold_bin, first)
    report["recent_by_holding_bin"] = grp(hold_bin, recent)

    # ⑤ 成本分解：by_entry_branch 的 seg_stats 已含 sum_gross_R / sum_cost_R / gross_le_cost_pct
    # 假信号率（⑥）
    def false_signal_stats(pool):
        if not pool:
            return {"n": 0}
        arr = np.array([r["net"] for r in pool])
        loss = arr < 0
        quick_loss = np.array([(r["net"] < 0 and abs(r["net"]) < 1.0) for r in pool])
        stop3 = np.array([(r["holding_bars"] <= 3 and r["net"] < 0) for r in pool])
        return {
            "n": len(pool),
            "loss_pct": round(float(loss.mean()) * 100, 2),
            "gross_le_cost_pct": round(float((np.array([r["gross"] for r in pool])
                                             <= np.array([r["cost"] for r in pool])).mean()) * 100, 2),
            "loss_within_1R_pct": round(float(quick_loss.mean()) * 100, 2),
            "stop_within_3bars_pct": round(float(stop3.mean()) * 100, 2),
        }
    report["false_signal_overall"] = false_signal_stats(rows)
    report["false_signal_recent"] = false_signal_stats(recent)
    _fs_branch: dict = defaultdict(list)
    for r in first:
        _fs_branch[branch_of(r)].append(r)
    report["false_signal_by_branch"] = {
        k: false_signal_stats(v) for k, v in _fs_branch.items()
    }

    # ⑦ 加码贡献
    report["addon_first"] = grp(lambda r: "addon" if r["addon"] else "first", rows)

    Path(args.out).write_text(
        json.dumps({"summary": report, "rows_count": len(rows)},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")

    # ---------------- 控制台摘要 ----------------
    def psec(tag, st):
        if not st or st.get("n", 0) == 0:
            print(f"  {tag}: (无样本)")
            return
        print(f"  {tag}: n={st['n']} 胜率={st['win_rate']}% 毛R={st['mean_gross_R']} "
              f"成本R={st['mean_cost_R']} 净R={st['mean_net_R']} "
              f"PF={st['profit_factor']} 毛<=成本占比={st['gross_le_cost_pct']}%")

    print("\n========== Alpha 层入场质量（OOS 已平仓成交）==========")
    print(f"样本：{len(rows)} 笔（早期<2024: {len(early)} / 近期>=2024: {len(recent)}）")
    psec("整体 OOS", report["overall"])
    psec("早期 OOS(<2024)", report["early_oos(<2024)"])
    psec("近期 OOS(>=2024)", report["recent_oos(>=2024)"])
    print("\n-- 按入场分支（首仓）--")
    for k, st in report["by_entry_branch"].items():
        psec(f"  {k}", st)
    print("-- 按多空 --")
    for k, st in report["by_side_first"].items():
        psec(f"  {k}", st)
    print("-- 按离场原因（近期 OOS）--")
    for k, st in report["recent_by_exit_reason"].items():
        psec(f"  {k}", st)
    print("-- 持有期分桶（首仓，近期 OOS）--")
    for k, st in report["recent_by_holding_bin"].items():
        psec(f"  {k}", st)
    print("-- 假信号率 --")
    for tag in ("false_signal_overall", "false_signal_recent"):
        fs = report[tag]
        print(f"  {tag}: {fs}")
    print(f"\n结果已写 {args.out}")


if __name__ == "__main__":
    main()
