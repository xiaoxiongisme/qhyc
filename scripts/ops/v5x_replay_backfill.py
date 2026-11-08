#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V5X.3 信号回放补录 —— 以 2026-11-05 夜盘为统计起点（CL-20261108-28）。

要解决什么
==========
V5X.3 在 2026-11-08 接管生产推送（CL-20261108-27），但「V5 到底表现如何」
需要一条从某个明确起点开始、完整不断的信号序列。用户拍板起点 =
**2026-11-05 夜盘（21:00 那根 bar）**。

V5X 是无状态回放扫描器（每轮重放 540 天，只取最后一根已收盘 bar 的订单事件），
所以「补历史」不需要重建任何引擎状态：把同一套 :func:`app.strategies.v5x.live.scan_range`
从起点扫到现在，逐根 bar 的事件就是当时会推出来的东西。

为什么不是「逐小时调 scan_once」
================================
38 品种 × 12 个小时点 = 456 次 540 天全量重放。而 `scan_range` 一次重放就能得到
**完全等价**的序列（scan_once 每轮也是从头重放、只取最后一根），差别只在耗时。
两者等价性有测试钉住（``tests/test_v5x_replay_parity.py``）。

落库与推送
==========
* 落 ``public.v5x_signal_log``，``source='replay'``；
* **不推送**。理由：一次性十几条历史信号进微信只会让人以为是新信号。
  要「当时的原始体验」，看对账报告的 Markdown 表；
* 去重键不含 source，所以「先回放、后实盘跑到同一根 bar」不会各记一条。

幂等：重复运行不会重复插行（``ON CONFLICT (dedupe_key) DO NOTHING``），
第二次跑 ``新增=0`` 是正确结果，不是失败。

用法
====
    # 干跑：只算不写，看条数与价格
    python scripts/ops/v5x_replay_backfill.py --dry-run

    # 真跑（默认起点 2026-11-05 21:00 上海时间），并出对账报告
    python scripts/ops/v5x_replay_backfill.py --report output/v5x_replay_20261108.md

    # 自定义区间
    python scripts/ops/v5x_replay_backfill.py --start "2026-11-05 21:00" --end "2026-11-08 23:59"
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 允许 `python scripts/ops/xxx.py` 直接跑（仓库根不在 sys.path 时补上）
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.db import session_scope                    # noqa: E402
from app.core.logging import logger                      # noqa: E402
from app.strategies.v5x.live import (                    # noqa: E402
    _persist,
    format_message,
    scan_range,
)

SH = ZoneInfo("Asia/Shanghai")
#: 统计起点：2026-11-05 夜盘 21:00（用户 2026-11-08 拍板）
DEFAULT_START = "2026-11-05 21:00"


def _parse(s: str) -> datetime:
    """解析 ``YYYY-MM-DD HH:MM`` 为上海时区 aware datetime。"""
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=SH)
        except ValueError:
            continue
    raise SystemExit(f"[fail-loud] 无法解析时间 {s!r}，期望 'YYYY-MM-DD HH:MM'")


def _fmt_ts(ts) -> str:
    t = ts if isinstance(ts, datetime) else ts.to_pydatetime()
    if t.tzinfo is not None:
        t = t.astimezone(SH)
    return t.strftime("%m-%d %H:%M")


def _build_report(sigs, new_keys: set[str], start: datetime, end: datetime,
                  existed: int) -> str:
    """生成 Markdown 对账报告（按时间正序，开平混排）。"""
    n_open = sum(1 for s in sigs if s.action == "OPEN")
    n_add = sum(1 for s in sigs if s.action == "ADD")
    n_close = sum(1 for s in sigs if s.action == "CLOSE")
    by_sym = Counter(s.symbol for s in sigs)

    lines: list[str] = []
    lines.append("# V5X.3 信号回放对账（2026-11-05 夜盘起）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(SH):%Y-%m-%d %H:%M:%S} (Asia/Shanghai)")
    lines.append(f"- 回放区间：{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}")
    lines.append(f"- 信号条数：**{len(sigs)}**（开仓 {n_open} / 加仓 {n_add} / 平仓 {n_close}）")
    lines.append(f"- 本次新增入库：**{len(new_keys)}** 条；已存在跳过：{existed} 条")
    lines.append(f"- 涉及品种：{len(by_sym)} 个 → "
                 + "、".join(f"{k}×{v}" for k, v in sorted(by_sym.items())))
    lines.append("")
    lines.append("> 口径：V5X.3 · t-1 门控 · BA 后复权信号 + 真实价反解 · 同根收盘成交假设。")
    lines.append("> 本表为**历史回放**结果（source=replay），当时**未推送**。")
    lines.append("")
    if not sigs:
        lines.append("（区间内无信号）")
        return "\n".join(lines) + "\n"

    lines.append("| # | bar 收盘 | 品种 | 动作 | 方向 | 真实价 | BA 价 | 偏移 | 止损(真实) | 手数 | 路径/原因 | 新增 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(sorted(sigs, key=lambda x: (x.bar_ts, x.symbol)), 1):
        note = s.reason or ("回踩" if s.etype == "pb" else
                            ("突破" if s.etype == "bk" else "-"))
        flag = "✓" if s.dedupe_key in new_keys else "已有"
        stop = f"{s.stop_real:,.2f}" if s.stop_real is not None else "-"
        lines.append(
            f"| {i} | {_fmt_ts(s.bar_ts)} | {s.symbol} | {s.action} | {s.side} | "
            f"{s.price_real:,.2f} | {s.price_ba:,.2f} | {s.adj_offset:+,.2f} | "
            f"{stop} | {s.lots:g} | {note} | {flag} |")
    lines.append("")
    lines.append("## 按品种")
    lines.append("")
    lines.append("| 品种 | 条数 | 开 | 加 | 平 |")
    lines.append("|---|---|---|---|---|")
    for sym in sorted(by_sym):
        sub = [s for s in sigs if s.symbol == sym]
        lines.append(f"| {sym} | {len(sub)} | "
                     f"{sum(1 for s in sub if s.action == 'OPEN')} | "
                     f"{sum(1 for s in sub if s.action == 'ADD')} | "
                     f"{sum(1 for s in sub if s.action == 'CLOSE')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="V5X.3 信号回放补录（不推送）")
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"回放起点（上海时间），默认 {DEFAULT_START}")
    ap.add_argument("--end", default=None, help="回放终点，默认此刻")
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    ap.add_argument("--report", default=None, help="对账报告输出路径（Markdown）")
    ap.add_argument("--preview", action="store_true",
                    help="按推送格式打印一遍（验证文案，不发送）")
    a = ap.parse_args(argv)

    start = _parse(a.start)
    end = _parse(a.end) if a.end else datetime.now(SH)

    logger.info(f"[replay] 区间 {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}"
                f"（dry_run={a.dry_run}）")
    with session_scope() as s:
        sigs = scan_range(s, start=start, end=end)
        if not sigs:
            logger.warning("[replay] 区间内无信号 → 不写库")
            if a.report:
                p = Path(a.report)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_build_report([], set(), start, end, 0), encoding="utf-8")
                logger.info(f"[replay] 报告已写 {p}")
            return 0

        if a.dry_run:
            # 干跑也要报告「哪些是新增」：查一遍去重键，不改表
            from sqlalchemy import text
            keys = [x.dedupe_key for x in sigs]
            rows = s.execute(
                text("SELECT dedupe_key FROM public.v5x_signal_log "
                     "WHERE dedupe_key = ANY(:ks)"), {"ks": keys}).fetchall()
            existing = {r[0] for r in rows}
            new_keys = {k for k in keys if k not in existing}
            logger.info(f"[replay] dry-run：共 {len(sigs)} 条，其中新增 {len(new_keys)}、"
                        f"已存在 {len(existing)}（未写库）")
        else:
            new = _persist(s, sigs, source="replay")
            s.commit()
            new_keys = {x.dedupe_key for x in new}
            logger.info(f"[replay] 已写库：新增 {len(new)} / 共 {len(sigs)}"
                        f"（重复 {len(sigs) - len(new)} 条被去重键挡下）")

        if a.preview and not a.dry_run:
            title, body = format_message(sigs[:20])
            logger.info(f"[replay] 文案预览（{len(sigs[:20])} 条）：\n{title}\n{body}")

        if a.report:
            p = Path(a.report)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                _build_report(sigs, new_keys, start, end, len(sigs) - len(new_keys)),
                encoding="utf-8")
            logger.info(f"[replay] 报告已写 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
