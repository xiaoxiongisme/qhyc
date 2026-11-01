# -*- coding: utf-8 -*-
"""在日线最优档（EMA250）下做逐品种 IS/OOS 同号筛选，并检查精简品种池后的组合表现。

动机
----
日线 EMA250 全品种 OOS 净 edge = -0.0127R（t=-0.37，已接近打平但为负）。
若亏损由少数品种贡献，则「周期修正 + 品种减法」两件正交的事叠加可能转正。

判据（沿用既有纪律：扫 N 个品种时 t>2 假阳性极多，只看双段同号）
    入选 = IS 净R > 0 且 OOS 净R > 0 且两段成交数均 >= 20
"""
from __future__ import annotations

import argparse
import io
import json
import sys

import numpy as np


def load(path: str) -> dict:
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="/tmp/ds_final.json")
    ap.add_argument("--key", default="daily_250")
    ap.add_argument("--min-n", type=int, default=20)
    args = ap.parse_args()

    d = load(args.json)
    if args.key not in d:
        print(f"可用 key: {list(d.keys())}")
        return 1
    node = d[args.key]
    isp = node["is"].get("per_symbol") or {}
    oosp = node["oos"].get("per_symbol") or {}

    common = sorted(set(isp) & set(oosp))
    print(f"=== {args.key} 逐品种 IS/OOS 同号筛选（min_n={args.min_n}）===")
    print(f"两段都有成交的品种数: {len(common)}")
    print()

    rows = []
    for s in common:
        a, b = isp[s], oosp[s]
        rows.append((s, a.get("n", 0), a.get("net_R", 0), a.get("pf", 0),
                     b.get("n", 0), b.get("net_R", 0), b.get("pf", 0),
                     b.get("pnl_yuan", 0)))
    # 按 OOS 净R 降序
    rows.sort(key=lambda r: r[5], reverse=True)

    print(f"{'品种':<8}{'IS n':>6}{'IS净R':>9}{'IS PF':>7}"
          f"{'OOS n':>7}{'OOS净R':>9}{'OOS PF':>8}{'OOS金额':>13}  判定")
    picked = []
    for s, an, anr, apf, bn, bnr, bpf, byuan in rows:
        ok = (an >= args.min_n and bn >= args.min_n and anr > 0 and bnr > 0)
        tag = "同号+" if ok else ""
        print(f"{s:<8}{an:>6}{anr:>9.4f}{apf:>7.2f}"
              f"{bn:>7}{bnr:>9.4f}{bpf:>8.2f}{byuan:>13,.0f}  {tag}")
        if ok:
            picked.append(s)

    print()
    print(f"--- 入选品种（IS/OOS 双段同号为正）: {len(picked)} 个 ---")
    print(picked if picked else "（无）")

    # 入选池 vs 全部：按 n 加权合成净 edge（近似组合效果）
    all_r = [r for r in rows]
    for label, sel in (("全部品种", all_r), ("入选池", [r for r in rows if r[0] in picked])):
        if not sel:
            continue
        tot_n = float(sum(r[4] for r in sel))
        wavg = float(sum(r[4] * r[5] for r in sel) / tot_n) if tot_n else 0.0
        yuan = float(sum(r[7] for r in sel))
        print(f"{label:<10} 品种数={len(sel):>3}  OOS笔数={int(tot_n):>5}  "
              f"按笔加权净R={wavg:+.4f}  OOS合计金额={yuan:+,.0f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
