#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C4 守卫：直接读**库里的数据**，断言 bar_60m 的桶标签属于会话网格。

为什么必须再加这一条
--------------------
2026-11-03 的事故（docs/V2样本外崩塌排查.md §36）里，两个既有守卫**全是绿的**：

* ``check_bar_grid_consistency.py`` 比对的是 ``synthesizer._SESSION_DAY_BOUNDS``
  与 ``rebuild_bars_session.DAY_BOUNDS`` 两份**代码常量** —— 它们确实一致；
* ``check_cross_tf_consistency.py`` 的窗口是「近 30 天」，而 11-02 起被写坏的
  数据当时还没进窗口（11-02 09:30 跑的时候当天桶尚未落盘）。

于是「代码是对的、库里是错的」这个分叉一路无人拦截，只有 11-03 的跨周期校验
偶然撞上「09:30 这个不该存在的桶」才报警。故补一条**直接读库**的断言：
60m 桶标签必须落在会话网格允许集内，否则 exit 1。

判据（60m 专用）
----------------
会话网格允许的 60m 桶标签集合（日盘 4 根 + 夜盘 6 根）::

    {02:00, 02:30, 10:00, 11:15, 14:15, 15:00, 22:00, 23:00, 00:00, 01:00}

被 ``time_bucket`` 自然整点写坏的网格会出现 09:00 / 11:00 / 13:00 / 14:00 / 21:00
（这些标签在会话网格里**永远不会**合法），故判 FAIL。

5m/15m/30m 不做此断言：它们的会话边界（11:15、14:15）本身就是 5/15/30 分钟的
整数倍，自然整点切分与会话切分**结果相同**，没有区分度（实测逐日行数逐位一致）。

用法::

    python scripts/validate/check_db_bar_grid.py [--days 3]

退出码：0=通过；1=发现违规；2=自身异常（拿不到 DB 等）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# 会话网格允许的 60m 桶标签（HH:MM）。日盘 10:00/11:15/14:15/15:00；
# 夜盘 21:00 开盘 → 22:00/23:00/00:00/01:00/02:00/02:30（各品种夜盘长短不同）。
ALLOWED_60M = {
    "00:00", "01:00", "02:00", "02:30",
    "10:00", "11:15", "14:15", "15:00",
    "22:00", "23:00",
}
# 自然整点 grid 独有的标签：在会话网格里永远不合法，一旦出现即为口径被改写。
W_ONLY_60M = {"09:00", "11:00", "13:00", "14:00", "21:00"}
# 会话网格独有标签：日盘收满的日子必须出现，否则说明退回了自然整点。
S_ONLY_60M = {"11:15", "14:15"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3,
                    help="抽检最近多少个自然日（默认 3）")
    args = ap.parse_args()

    try:
        import psycopg  # noqa: PLC0415
        from app.core.config import get_settings  # noqa: PLC0415

        url = get_settings().db_url.replace("postgresql+psycopg://", "postgresql://")
    except Exception as e:  # noqa: BLE001
        print(f"[C4][ERROR] 无法加载配置：{type(e).__name__}: {e}")
        return 2

    try:
        with psycopg.connect(url) as c:
            c.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            rows = c.execute(
                """
                SELECT bucket::date AS d,
                       to_char(bucket, 'HH24:MI') AS t,
                       count(*) AS n
                FROM l1_mkt.bar_60m
                WHERE bucket >= current_date - %s
                GROUP BY 1, 2
                ORDER BY 1, 2
                """,
                (args.days,),
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"[C4][ERROR] 查询失败：{type(e).__name__}: {e}")
        return 2

    if not rows:
        print(f"[C4][WARN] 最近 {args.days} 天 l1_mkt.bar_60m 无数据，跳过（不判失败）")
        return 0

    by_day: dict = {}
    for d, t, n in rows:
        by_day.setdefault(d, {})[t] = n

    bad_labels: list[str] = []
    missing_s: list[str] = []
    for d in sorted(by_day):
        labels = by_day[d]
        unknown = sorted(set(labels) - ALLOWED_60M - W_ONLY_60M)
        w_hit = sorted(set(labels) & W_ONLY_60M)
        if w_hit or unknown:
            detail = []
            if w_hit:
                detail.append("自然整点桶 " + ", ".join(f"{t}({labels[t]})" for t in w_hit))
            if unknown:
                detail.append("未知桶 " + ", ".join(f"{t}({labels[t]})" for t in unknown))
            bad_labels.append(f"{d}: " + "；".join(detail))
        # 日盘收满（有 15:00 桶）却缺 11:15/14:15 → 退回自然整点
        if labels.get("15:00") and not all(labels.get(t) for t in S_ONLY_60M):
            miss = [t for t in sorted(S_ONLY_60M) if not labels.get(t)]
            missing_s.append(f"{d}: 缺 {', '.join(miss)}")

    if bad_labels or missing_s:
        print(f"[C4][FAIL] bar_60m 网格不符合会话口径（近 {args.days} 天）")
        for x in bad_labels:
            print(f"  - 非法桶标签 {x}")
        for x in missing_s:
            print(f"  - 会话桶缺失 {x}")
        print("  说明：60m 合法标签 = {00:00,01:00,02:00,02:30,10:00,11:15,14:15,15:00,22:00,23:00}")
        print("  处置：确认 ingest.bar_synth_mode='python'，再跑 rebuild/synthesize 重建近 4 天")
        return 1

    print(f"[C4][OK] bar_60m 近 {args.days} 天网格符合会话口径"
          f"（{len(by_day)} 个交易日，标签均在允许集内）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
