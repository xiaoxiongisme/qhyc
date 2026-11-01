#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 跨品种配对均值回复信号（2026-11-01，研究分支，未碰 main/cb/dev）。

与 V2 趋势正交：截面均值回复（价差偏离→回归），非方向博弈。在板块内高相关品种上做
市场中性价差：z = (ratio - rolling_mean)/rolling_std，z 过高→空价差（空 A 多 B），
z 过低→多价差（多 A 空 B），|z|<exit_z 平仓。整体 shift(1) 无前视，rebalance=1（日频，
由状态机自管进出）。

配对（手写、互斥，确保每品种只在一个对里；运行时跳过缺失品种）：
  螺纹-热卷 RB-HC / 豆粕-菜粕 M-RM / 豆油-棕榈 Y-P / PTA-甲醇 TA-MA /
  玻璃-纯碱 FG-SA / 焦炭-焦煤 J-JM / 聚丙烯-塑料 PP-L / 金-银 AU-AG
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

PAIRS = [
    ("RB", "HC"), ("M", "RM"), ("Y", "P"), ("TA", "MA"),
    ("FG", "SA"), ("J", "JM"), ("PP", "L"), ("AU", "AG"),
]


def compute_targets(closes: pd.DataFrame, opens=None, window: int = 60,
                    entry_z: float = 2.0, exit_z: float = 0.5,
                    pairs=None) -> pd.DataFrame:
    """pairs=None → 默认 PAIRS（基础 8 对，保持与 §9 准入结论逐位可比）；
    传入自定义互斥对列表用于扩配对敏感性验证（见 signal_b3_grid.py）。"""
    if pairs is None:
        pairs = PAIRS
    targets = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    active = 0
    for va, vb in pairs:
        sa, sb = va + "888", vb + "888"
        if sa not in closes.columns or sb not in closes.columns:
            continue
        ra, rb = closes[sa], closes[sb]
        ratio = ra / rb
        m = ratio.rolling(window, min_periods=max(10, window // 2)).mean()
        sd = ratio.rolling(window, min_periods=max(10, window // 2)).std()
        z = ((ratio - m) / sd).shift(1)  # 无前视
        state = 0
        for d in z.index:
            zv = z.at[d]
            if pd.isna(zv):
                targets.at[d, sa] = state
                targets.at[d, sb] = -state
                continue
            if state == 0:
                if zv > entry_z:
                    state = -1
                elif zv < -entry_z:
                    state = 1
            else:
                if abs(zv) < exit_z:
                    state = 0
            targets.at[d, sa] = state
            targets.at[d, sb] = -state
        active += 1
    # 注：PAIRS 已互斥，不会冲突
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--rebalance-n", type=int, default=1)
    ap.add_argument("--no-placebo", dest="do_placebo", action="store_false")
    ap.add_argument("--n-perm", type=int, default=300)
    ap.add_argument("--out", default="/tmp/b3_pairs.json")
    args = ap.parse_args()

    from app.core.config import get_settings
    symbols = [s.symbol for s in get_settings().main_contracts] or []

    def sig(closes, opens=None, **kw):
        return compute_targets(closes, opens=opens, window=args.window,
                               entry_z=args.entry_z, exit_z=args.exit_z)

    report = run_signal_wf(sig, "B3_pairs_mean_reversion",
                           symbols,
                           date.fromisoformat(args.start),
                           date.fromisoformat(args.end),
                           rebalance_n=args.rebalance_n, out=args.out,
                           do_placebo=args.do_placebo, n_perm=args.n_perm,
                           window=args.window, entry_z=args.entry_z,
                           exit_z=args.exit_z)
    print_report(report)


if __name__ == "__main__":
    main()
