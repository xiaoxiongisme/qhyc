"""在线小时线采集 CLI

用法：
  # 全市场 73 品种（默认 akshare 主源，免费免登录，增量刷新）
  python scripts/collect_hourly.py --all

  # 单品种
  python scripts/collect_hourly.py --symbol FG888

  # 指定品种（按 product）
  python scripts/collect_hourly.py --product SA

  # tqsdk 长历史回填（data_length 控制拉取根数，约 8000 根 ≈ 1 年小时线）
  python scripts/collect_hourly.py --all --prefer tqsdk --data-length 8000

说明：
- 写入 hourly_bar，按 (symbol, trade_datetime) 幂等 upsert，可反复跑。
- 调度器已在每个交易日 16:00（收盘）后自动跑一次 --all（见 app.scheduler）。
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="在线小时线采集（akshare/tqsdk）")
    ap.add_argument("--all", action="store_true", help="采集全部主连品种")
    ap.add_argument("--symbol", help="单品种主连代码，如 FG888")
    ap.add_argument("--product", help="按品种字母，如 SA（与 --symbol 二选一）")
    ap.add_argument(
        "--prefer",
        default="akshare",
        choices=["akshare", "tqsdk"],
        help="主源：akshare（默认，免费）| tqsdk（长历史回填）",
    )
    ap.add_argument(
        "--data-length",
        type=int,
        default=8000,
        help="tqsdk 拉取根数（仅 --prefer tqsdk 生效）",
    )
    args = ap.parse_args()

    from app.core.config import get_settings
    from app.core.db import session_scope
    from app.ingest.hourly_collector import HourlyCollector

    specs = get_settings().main_contracts
    if args.symbol:
        specs = [s for s in specs if s.symbol == args.symbol]
    elif args.product:
        specs = [s for s in specs if s.product == args.product]
    elif not args.all:
        ap.error("请指定 --all / --symbol / --product 之一")

    if not specs:
        print("未匹配到品种，退出")
        return 2

    with session_scope() as s:
        hc = HourlyCollector(s, prefer=args.prefer)
        if len(specs) == 1 and (args.symbol or args.product):
            n = hc.collect_symbol(specs[0], data_length=args.data_length)
            print(f"{specs[0].symbol}: {n} rows")
        else:
            stats = hc.collect_all(
                only_products=([sp.product for sp in specs] if (args.symbol or args.product) else None),
                data_length=args.data_length,
            )
            total = sum(r.get("rows", 0) for r in stats if "error" not in r)
            errs = [r for r in stats if "error" in r]
            print(f"完成：{len(stats)} 品种，写入 {total} 行" + (f"，失败 {len(errs)}" if errs else ""))
            for r in errs:
                print(f"  FAIL {r['symbol']}: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
