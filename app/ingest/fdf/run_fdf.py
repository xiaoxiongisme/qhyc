# -*- coding: utf-8 -*-
"""全品种 1h / 15min 一键入库 + 复权编排器。

对每个品种、每个频率：先抓主连+分合约（fetch_fdf），再生成复权主连（adjust_fdf）。
按 (freq, symbol) 的 cont_adj 是否已存在做断点续跑：已存在则整段跳过，
因此中断后重跑只补未完成的品种。

用法：
  python -m app.ingest.fdf.run_fdf                 # 全品种 hourly + min15
  python -m app.ingest.fdf.run_fdf --freq hourly   # 仅 1h
  python -m app.ingest.fdf.run_fdf --symbol CZCE.FG
"""
from __future__ import annotations

import argparse
import sys

from app.ingest.fdf import fetch_fdf as F
from app.ingest.fdf import adjust_fdf as A
from app.ingest.fdf import db_pg as D


def run_all(freqs, symbols=None, start="2020-01-01", full=False, tail=False):
    freqs = freqs or ["hourly", "min15"]
    syms = symbols or F.all_symbols()
    for freq in freqs:
        print(f"\n########## 频率 {freq} ##########")
        for key in syms:
            spec = F.SYM.get(key)
            if not full and D.latest_date(freq, "cont_adj", spec["tq_cont"]) is not None:
                print(f"[续跑跳过] {spec['name']}（{key}）{freq} 复权主连已存在")
                continue
            try:
                F.run([freq], symbols=[key], start=start, full=full, tail=tail)
                A.run(key, freq)
                print(f"[完成] {spec['name']}（{key}）{freq} 抓取+复权\n")
            except Exception as e:  # noqa: BLE001
                print(f"[异常] {spec['name']}（{key}）{freq}: {type(e).__name__}: {e}")
                print("  继续下一个品种…")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="全品种 1h/15min 入库+复权编排器")
    ap.add_argument("--freq", default=None)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--tail", action="store_true")
    args = ap.parse_args()
    fs = [args.freq] if args.freq else ["hourly", "min15"]
    syms = [args.symbol] if args.symbol else None
    run_all(fs, symbols=syms, start=args.start, full=args.full, tail=args.tail)
