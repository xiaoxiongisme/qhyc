# -*- coding: utf-8 -*-
"""2026-01-01 ~ 09-30 行情补采骨架（云端执行）。

背景（用户 2026-09-30）
----------------------
当前 1 分钟数据缺失且无法补充；5/15/30/60m 与日线需在 2026-01-01~09-30 区间补齐。
本脚本是**采集调度骨架**：按 品种 × 周期 循环调用采集器写入 L0，再触发 L1/L2 存储过程，
最后由 sync_cloud_local 反向灌入本地。

执行位置：云端（唯一真源，有 tqsdk/akshare 凭证与全量 bar_*）。
本地只做设计，不跑本脚本。

用法
----
    # dry-run：只打印将补采的品种/周期，不写库
    python scripts/backfill_bars_2026.py --dry-run \
        --start 2026-01-01 --end 2026-09-30 --freqs 5m,15m,30m,60m,daily

    # 实跑（先在小品种集验证）
    python scripts/backfill_bars_2026.py --start 2026-01-01 --end 2026-09-30 \
        --freqs 15m --products FG CU --apply

采集器接入说明
--------------
本骨架把「单品种单周期采集」抽象为 `collect_frequency(symbol, freq, start, end)`。
请在该函数内接入实际采集源（推荐 tqsdk 直接拉各周期 K 线，或 akshare 日/小时线），
写入对应 L0 表：
  - 5/15/30/60m → bar_<X>m（作为 L1，由 sp 归一化；或直接写 L0 fut_kline 对应 freq×kind）
  - daily       → daily_bar（akshare 主源）
  - hourly      → hourly_bar（akshare）
具体 tqsdk/akshare 调用请参照既有 scripts/collect_*.py 与 app/ingest/*。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_2026")

# 受补周期 → (bar 表 / roll_segment.freq)
FREQ_MAP = {
    "5m": ("bar_5m", "min5"),
    "15m": ("bar_15m", "min15"),
    "30m": ("bar_30m", "min30"),
    "60m": ("bar_60m", "min60"),
    "daily": ("daily_bar", None),
    "hourly": ("hourly_bar", None),
}


def collect_frequency(symbol: str, freq: str, start: str, end: str) -> int:
    """采集单品种单周期写入 L0。

    ⚠ TODO（云端接线）：在此接入 tqsdk / akshare 实际拉取。
    返回写入行数。未接线时抛 NotImplementedError，避免误写空数据。
    """
    raise NotImplementedError(
        f"collect_frequency({symbol},{freq}) 未接线：请在云端参照 "
        f"scripts/collect_*.py / app/ingest/* 实现 tqsdk/akshare 拉取并写入 L0。"
    )


def refresh_l1_l2(freq: str, session) -> None:
    """触发 L1 归一化 + L2 复权刷新（仅非日线/小时线）。"""
    from sqlalchemy import text
    if freq in ("5m", "15m", "30m", "60m"):
        rs_freq = FREQ_MAP[freq][1]
        logger.info(f"[refresh] sp_build_l2_roll_segment('{rs_freq}')")
        session.execute(text(f"CALL sp_build_l2_roll_segment('{rs_freq}')"))
        session.commit()


def run(args) -> int:
    freqs = [f.strip() for f in args.freqs.split(",") if f.strip() in FREQ_MAP]
    if not freqs:
        logger.error(f"--freqs 含非法周期，合法值：{','.join(FREQ_MAP)}")
        return 2

    products = [p.strip().upper() for p in args.products.split(",")] if args.products else None

    # 品种清单：来自 dim_variety（优先）或 futures_symbol
    try:
        from app.core.db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            if products:
                syms = products
            else:
                syms = [r[0] for r in conn.execute(
                    text("SELECT variety_code FROM dim_variety WHERE is_active ORDER BY variety_code")
                ).all()]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"取品种清单失败（用 --products 指定）：{e}")
        if not products:
            return 3
        syms = products

    logger.info(f"补采计划：{len(syms)} 品种 × {freqs} | {args.start}~{args.end} | "
                f"{'DRY-RUN' if args.dry_run else 'APPLY'}")
    for freq in freqs:
        for sym in syms:
            if args.dry_run:
                logger.info(f"  [dry] collect {sym} {freq} {args.start}~{args.end}")
                continue
            try:
                n = collect_frequency(sym, freq, args.start, args.end)
                logger.info(f"  [ok] {sym} {freq} 写入 {n} 行")
            except NotImplementedError as nie:
                logger.error(f"  [skip] {sym} {freq}: {nie}")
                return 4
            except Exception as e:  # noqa: BLE001
                logger.exception(f"  [fail] {sym} {freq}: {e}")
                return 5

    if not args.dry_run:
        # L1/L2 刷新
        from app.core.db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            for freq in freqs:
                refresh_l1_l2(freq, conn)
        logger.info("L1/L2 刷新完成；下一步：sync_cloud_local 反向灌入本地。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2026 行情补采骨架（云端执行）")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--freqs", default="5m,15m,30m,60m,daily",
                    help="逗号分隔；合法值见 FREQ_MAP")
    ap.add_argument("--products", default=None,
                    help="逗号分隔品种码（默认全量 dim_variety）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不写库")
    ap.add_argument("--apply", dest="dry_run", action="store_false",
                    help="实际写入（需先接线 collect_frequency）")
    ap.set_defaults(dry_run=True)
    args = ap.parse_args()
    # 校验日期
    try:
        date.fromisoformat(args.start); date.fromisoformat(args.end)
    except ValueError:
        logger.error("--start/--end 须为 YYYY-MM-DD"); return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
