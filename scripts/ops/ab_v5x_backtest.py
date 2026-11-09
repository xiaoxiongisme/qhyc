"""V5X 全池回测 A/B 对拍（CL-20261108-05 / -06 的验收前提）。

用途：换月段表四点统一重算前后，用**同一份代码、同一窗口**各跑一次
V5X（38 白名单 / 2015-01-01~2026-11-07 / ``bar_60m_ba`` / E1-C next_open），
证明重算只改变「换月边界的偏移」，不改变策略结论。

口径必须显式写死，否则对拍无意义：
* ``src=bar_60m_ba``（V5X 实盘与冻结版都吃后复权 60m）
* ``fill_mode=next_open`` + 1tick + 0.5ATR 跳空拒绝（CL-20261109-05）
* ``gate_lag=1``（小时级实盘口径，报告里的 strict_lag1）
* 三道上下文门全开（hvol/blocked/regime），P6 开启

用法：
    python scripts/ops/ab_v5x_backtest.py --tag before
    python scripts/ops/ab_v5x_backtest.py --tag after
    python scripts/ops/ab_v5x_backtest.py --compare before after
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from app.backtest.v5x_backtest import V5XBacktestParams, run_v5x_backtest
from app.core.db import session_scope

OUT_DIR = Path("/app/output/ab")
BASELINE = {"n_trades": 8580, "net_yuan": 342152.0}


def run(tag: str, start: date, end: date, gate_lag: int, regime_mode: str) -> dict:
    params = V5XBacktestParams(
        src="bar_60m_ba",
        fill_mode="next_open",
        fill_slip_ticks=1.0,
        max_adverse_gap_atr=0.5,
        gate_lag=gate_lag,
        regime_mode=regime_mode,
        use_hvol=True, use_blocked=True, use_regime=True, use_p6=True,
        note=f"ab:{tag}",
    )
    with session_scope() as s:
        agg = run_v5x_backtest(s, symbols=None, start=start, end=end, params=params)
    keep = {k: agg[k] for k in ("n_trades", "net_yuan", "gross_yuan", "win_rate",
                                "avg_r", "profit_factor", "fill_mode", "fill_stats",
                                "gate_lag", "regime_mode", "yearly", "per_symbol", "skipped")}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"v5x_{tag}.json"
    path.write_text(json.dumps(keep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"[{tag}] 笔数={keep['n_trades']} 净利={keep['net_yuan']:,.0f} "
          f"胜率={keep['win_rate']} PF={keep['profit_factor']} → {path}")
    return keep


def compare(t1: str, t2: str) -> int:
    a = json.loads((OUT_DIR / f"v5x_{t1}.json").read_text(encoding="utf-8"))
    b = json.loads((OUT_DIR / f"v5x_{t2}.json").read_text(encoding="utf-8"))
    print(f"\n{'指标':12s}{t1:>16s}{t2:>16s}{'差':>14s}")
    for k, fmt in (("n_trades", "{:,}"), ("net_yuan", "{:,.0f}"),
                   ("win_rate", "{:.4f}"), ("profit_factor", "{:.4f}")):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            print(f"{k:12s}{fmt.format(va):>16s}{fmt.format(vb):>16s}{vb - va:>+14,.4f}")
        else:
            print(f"{k:12s}{str(va):>16s}{str(vb):>16s}")

    pa = {r["symbol"]: r for r in a.get("per_symbol", [])}
    pb = {r["symbol"]: r for r in b.get("per_symbol", [])}
    diff = [(s, pa[s]["net_yuan"], pb[s]["net_yuan"]) for s in sorted(set(pa) | set(pb))
            if abs(pa.get(s, {}).get("net_yuan", 0) - pb.get(s, {}).get("net_yuan", 0)) > 1]
    if diff:
        print(f"\n净利有变化的品种（{len(diff)} 个）：")
        for s, x, y in sorted(diff, key=lambda r: -abs(r[2] - r[1])):
            print(f"  {s:8s} {x:>12,.0f} → {y:>12,.0f}  ({y - x:+,.0f})")
    else:
        print("\n38 品种逐品种净利完全一致（差异为 0）。")

    ya, yb = a.get("yearly", {}), b.get("yearly", {})
    yd = [(y, ya.get(y, {}).get("net", 0), yb.get(y, {}).get("net", 0)) for y in sorted(set(ya) | set(yb))]
    yd = [r for r in yd if abs(r[2] - r[1]) > 1]
    if yd:
        print("\n分年有变化：")
        for y, x, z in yd:
            print(f"  {y} {x:>12,.0f} → {z:>12,.0f}  ({z - x:+,.0f})")
    else:
        print("分年净利完全一致。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--compare", nargs=2, metavar=("TAG1", "TAG2"))
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-11-07")
    ap.add_argument("--gate-lag", type=int, default=1)
    # gate_lag=1 必须配 strict：frozen 的全样本分位含前视（代码里硬校验）
    ap.add_argument("--regime-mode", default="strict", choices=["frozen", "strict"])
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    if not args.tag:
        print("需要 --tag 或 --compare", file=sys.stderr)
        return 2
    keep = run(args.tag, date.fromisoformat(args.start), date.fromisoformat(args.end),
               args.gate_lag, args.regime_mode)
    if args.tag == "after" and keep["n_trades"] != BASELINE["n_trades"]:
        print(f"⚠ 与归档基线笔数不一致：{keep['n_trades']} vs {BASELINE['n_trades']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
