"""fut_kline **增量**入库（天勤 tqsdk）——补上缺失的定时增量链路（2026-09-23）。

背景（实测 2026-09-23）：
  fut_kline（freq × kind = daily/hourly/min15 × contract/continuous/cont_adj）
  最晚数据停在 2026-09-11~09-16，而 daily_bar/hourly_bar（akshare）正常。
  根因：scheduler 只注册了 `adjust_fdf`（02:30 由 fut_kline 生成 cont_adj），
  **没有任何抓取原始行情（fetch_fdf）的定时任务** → 原始层不再有增量，
  adjust 只能在陈数据上重算。本脚本即补上这一环。

设计：
  1. 从 DB 读每个 freq 的 max(trade_datetime)，往前回退 buffer_days 天作为回补窗口
     （留缓冲，防止换月/补写漏根）
  2. 调 `app.ingest.fdf.fetch_fdf.run()` 抓取并 **upsert**（save_bars 幂等：同 PK 覆盖）
  3. 可选 `--adjust`：抓完后跑 adjust_fdf 生成 cont_adj（复权主连）
  4. `--max-stale-days N`：数据落后不超过 N 天则直接跳过（定时任务防重复跑）

用法：
  python scripts/ingest_fut_kline_incremental.py                    # daily+hourly 增量
  python scripts/ingest_fut_kline_incremental.py --freqs daily       # 只补日线
  python scripts/ingest_fut_kline_incremental.py --adjust            # 抓完顺带生成复权主连
  python scripts/ingest_fut_kline_incremental.py --dry-run           # 只看窗口不抓数
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# scripts/ 下直接运行时自举仓库根（与容器内 /app 一致）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import func, select

from app.core.db import session_scope
from app.core.logging import logger
from app.models import FutKline


def last_dt(freq: str) -> datetime | None:
    """该 freq 当前最晚一根的时间（跨 kind 取 max）。"""
    with session_scope() as s:
        return s.execute(
            select(func.max(FutKline.trade_datetime)).where(FutKline.freq == freq)
        ).scalar()


def last_dt_by_kind(freq: str) -> dict[str, str]:
    with session_scope() as s:
        rows = s.execute(
            select(FutKline.kind, func.max(FutKline.trade_datetime))
            .where(FutKline.freq == freq)
            .group_by(FutKline.kind)
        ).all()
    return {k: (v.isoformat() if v else "-") for k, v in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="fut_kline 增量入库（天勤 tqsdk）")
    ap.add_argument("--freqs", default="daily,hourly", help="逗号分隔：daily/hourly/min15")
    ap.add_argument("--buffer-days", type=int, default=7, help="回补窗口向前缓冲天数")
    ap.add_argument("--symbols", default="", help="限定品种 key（逗号分隔），默认全部")
    ap.add_argument("--adjust", action="store_true", help="抓完后跑 adjust_fdf 生成 cont_adj")
    ap.add_argument("--max-stale-days", type=int, default=0,
                    help=">0：数据落后不超过该天数则跳过（防重复跑）")
    ap.add_argument("--dry-run", action="store_true", help="只打印窗口，不抓数")
    a = ap.parse_args()

    freqs = [f.strip() for f in a.freqs.split(",") if f.strip()]
    print("=== fut_kline 增量入库 ===")
    years = []
    for f in freqs:
        lt = last_dt(f)
        detail = last_dt_by_kind(f)
        if lt is None:
            print(f"  {f}: 无数据 → 按默认起始年回补")
            years.append(date.today().year - 1)  # 无数据时至少补近两年
            years.append(date.today().year)
            continue
        d = lt.date() if isinstance(lt, datetime) else lt
        stale = (date.today() - d).days
        print(f"  {f}: 最晚={d} 落后={stale}天  {detail}")
        if a.max_stale_days and stale <= a.max_stale_days:
            print(f"  {f}: 落后 ≤ {a.max_stale_days} 天，跳过")
        years.append((d - timedelta(days=a.buffer_days)).year)

    if a.dry_run:
        print(f"[dry-run] freqs={freqs} start_year={min(years)} 未抓数")
        return 0

    from app.ingest.fdf import fetch_fdf as F

    start_year = str(min(years))
    print(f"开始抓取：freqs={freqs} start_year={start_year} "
          f"(run() 按年粒度回补，save_bars 幂等覆盖)")
    syms = [x.strip() for x in a.symbols.split(",") if x.strip()] or None
    F.run(freqs=freqs, symbols=syms, start=start_year, full=False)

    if a.adjust:
        from app.core.config import get_settings
        from app.ingest.fdf import adjust_fdf

        ok = fail = 0
        for spec in get_settings().main_contracts:
            key = f"{spec.exchange}.{spec.product.lower()}"
            for freq in freqs:
                if freq == "min15":
                    continue
                try:
                    adjust_fdf.run(key, freq)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    logger.warning(f"[fut_kline] adjust {key} {freq} failed: {e}")
        print(f"adjust_fdf 完成 ok={ok} fail={fail}")

    print("=== 抓取后新鲜度 ===")
    for f in freqs:
        lt = last_dt(f)
        print(f"  {f}: 最晚={lt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
