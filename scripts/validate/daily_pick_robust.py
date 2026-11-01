# -*- coding: utf-8 -*-
"""稳健性交叉检验：候选品种池是否只在某一个参数下成立（=过拟合），
还是跨周期尺度普遍成立（=品种自身属性）。

判据
----
对每个候选品种，检查它在 5 个配置（daily 60/120/180/250 + hourly 140）下的
OOS 净R 符号。统计「正号命中数」：
    5/5 或 4/5  -> 稳健，可视为品种属性
    <=3/5       -> 参数敏感，大概率是多重检验假阳性
"""
from __future__ import annotations

import argparse
import io
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="/tmp/ds_final.json")
    ap.add_argument("--cands", nargs="+",
                    default=["EB888", "CU888", "CF888", "SC888",
                             "SA888", "P888", "A888", "SM888"])
    ap.add_argument("--keys", nargs="+",
                    default=["daily_60", "daily_120", "daily_180",
                             "daily_250", "hourly_140"])
    args = ap.parse_args()

    with io.open(args.json, encoding="utf-8") as f:
        d = json.load(f)

    avail = [k for k in args.keys if k in d]
    print("=== 候选品种跨口径 OOS 净R（稳健性交叉检验）===")
    print("配置:", ", ".join(avail))
    print()

    hdr = f"{'品种':<9}" + "".join(f"{k.replace('daily_', 'D').replace('hourly_', 'H'):>9}"
                                   for k in avail) + f"{'正号数':>8}  判定"
    print(hdr)
    print("-" * len(hdr))

    summary = []
    for sym in args.cands:
        cells, pos = [], 0
        for k in avail:
            ps = (d[k]["oos"].get("per_symbol") or {}).get(sym)
            if not ps:
                cells.append("  n/a")
                continue
            v = ps.get("net_R", 0.0)
            n = ps.get("n", 0)
            cells.append(f"{v:>+9.3f}")
            if v > 0:
                pos += 1
        tot = len([c for c in cells if c.strip() != "n/a"])
        ratio = pos / tot if tot else 0.0
        verdict = "稳健" if ratio >= 0.8 else ("偏弱" if ratio >= 0.6 else "敏感/存疑")
        print(f"{sym:<9}" + "".join(cells) + f"{pos}/{tot}".rjust(8) + f"  {verdict}")
        summary.append((sym, pos, tot, ratio))

    print()
    robust = [s for s, p, t, r in summary if r >= 0.8]
    weak = [s for s, p, t, r in summary if 0.6 <= r < 0.8]
    bad = [s for s, p, t, r in summary if r < 0.6]
    print(f"稳健(>=80%正号): {robust if robust else '（无）'}")
    print(f"偏弱(60-80%):    {weak if weak else '（无）'}")
    print(f"敏感(<60%):      {bad if bad else '（无）'}")

    # 补充：同一批品种在**小时线生产口径**下的表现（用户现在跑的就是它）
    if "hourly_140" in d:
        print()
        print("=== 对照：这些品种在【当前生产小时线】口径下的 OOS 表现 ===")
        hp = d["hourly_140"]["oos"].get("per_symbol") or {}
        hip = d["hourly_140"]["is"].get("per_symbol") or {}
        for sym in args.cands:
            a, b = hip.get(sym), hp.get(sym)
            if not a or not b:
                print(f"  {sym:<9} 数据缺失")
                continue
            print(f"  {sym:<9} IS n={a.get('n',0):>4} 净R={a.get('net_R',0):>+7.3f} | "
                  f"OOS n={b.get('n',0):>4} 净R={b.get('net_R',0):>+7.3f} "
                  f"PF={b.get('pf',0):>5.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
