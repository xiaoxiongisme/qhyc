#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 截面动量信号（替代不可行的基差 carry；2026-11-01，研究分支，未碰 main/cb/dev）。

与 V2 单品种趋势完全正交：V2 是「每个品种各看各的突破/回踩」，本信号是「横向比较 50
品种过去 N 日收益、多强空弱」——相对排名，不依赖绝对方向。若边真实，应与 V2 低相关。

逻辑：ret[t] = close[t]/close[t-lookback] - 1，整体 shift(1)（target[t] 只用 ≤t-1 数据，
次日 open 执行，杜绝前视）；按行分位，top-q 赋 +1、bottom-q 赋 -1，中间 0。
参数：lookback=60 日，q=0.2（多/空各 20%，中间 40% 空仓）。rebalance=21（月频）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from signal_oos_backtest import run_signal_wf, print_report


def compute_targets(closes: pd.DataFrame, opens=None, lookback: int = 60,
                    q: float = 0.2, min_valid: int = 10) -> pd.DataFrame:
    ret = (closes / closes.shift(lookback) - 1.0).shift(1)
    pct = ret.rank(axis=1, pct=True)
    valid = ret.notna().sum(axis=1)
    targets = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    hi = (pct >= (1 - q)) & ret.notna()
    lo = (pct <= q) & ret.notna()
    targets[hi] = 1.0
    targets[lo] = -1.0
    targets[valid < min_valid] = 0.0
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--lookback", type=int, default=60)
    ap.add_argument("--q", type=float, default=0.2)
    ap.add_argument("--rebalance-n", type=int, default=21)
    ap.add_argument("--no-placebo", dest="do_placebo", action="store_false")
    ap.add_argument("--n-perm", type=int, default=300)
    ap.add_argument("--out", default="/tmp/b1_momentum.json")
    args = ap.parse_args()

    from app.core.config import get_settings
    from app.backtest.fusion_backtest import fusion_params_from_config  # noqa
    symbols = [s.symbol for s in get_settings().main_contracts] or []

    # 信号参数随 kwargs 传入底座
    def sig(closes, opens=None, **kw):
        return compute_targets(closes, opens=opens, lookback=args.lookback,
                               q=args.q)

    report = run_signal_wf(sig, "B1_cross_sectional_momentum",
                           symbols,
                           date.fromisoformat(args.start),
                           date.fromisoformat(args.end),
                           rebalance_n=args.rebalance_n, out=args.out,
                           do_placebo=args.do_placebo, n_perm=args.n_perm,
                           lookback=args.lookback, q=args.q)
    print_report(report)


if __name__ == "__main__":
    main()
