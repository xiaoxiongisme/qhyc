# -*- coding: utf-8 -*-
"""对最终清单全部品种做一次复权（forward 加法平移前复权）。

用法：
  python -m app.ingest.fdf.adjust_all            # hourly
  python -m app.ingest.fdf.adjust_all --freq min15

用于"数据获取完成后"的统一复权收尾：逐个品种调用 adjust_fdf.run，
任一品种失败不影响其它品种（打印后继续）。
"""
from __future__ import annotations

import argparse
import sys

from app.ingest.fdf import symbols as SYM
from app.ingest.fdf import adjust_fdf as A
from app.ingest.fdf import db_pg as D


def main(freq="hourly"):
    keys = list(SYM.TABLE.keys())
    ok = fail = 0
    print(f"[复权收尾] 频率={freq}，品种数={len(keys)}")
    for key in keys:
        spec = SYM.get(key)
        try:
            A.run(key, freq)
            ok += 1
            print(f"[复权完成] {spec['name']}（{key}）{freq}\n")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"[复权异常] {spec['name']}（{key}）{freq}: {type(e).__name__}: {e}")
            print("  继续下一个品种…\n")
    print(f"[复权收尾] 结束：成功 {ok} / 失败 {fail} / 共 {len(keys)}")
    return ok, fail


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="全部品种复权收尾")
    ap.add_argument("--freq", default="hourly", help="hourly/min15/daily")
    args = ap.parse_args()
    D.ensure_schema([args.freq])
    main(args.freq)
