# -*- coding: utf-8 -*-
"""DCE 会员持仓排名回填驱动器（2020-2022 缺口）。

直接复用 scrapling-qi 技能已验证的 ``DCE_scrapling_crawler``（自带瑞数绕行 /
分块换会话 / 增量落库 / 断点续传），仅把其 ``PG`` 连接指向目标库：

  - 默认指向云端（经本机 SSH 隧道 127.0.0.1:15432 -> 云端 timescaledb:5432）
  - 设 DCE_PG_PORT=5432 即指向 CB 本地库

落库口径与 qhyc 既有 ``member_position_rank`` 完全一致
（PK: trade_date+exchange+symbol+member+version；src=scrapling:dce_memberDealPosi）。

用法：
  # 验证瑞数抓取+解析（不写库）
  python scripts/dce_backfill.py --no-db --start 20210601 --end 20210601

  # 真实回填云端 2020-2022（后台跑，断点续传；中断后重跑同命令即续）
  python scripts/dce_backfill.py --start 20200101 --end 20221231

  # 同样区间写本地（如需）
  DCE_PG_PORT=5432 python scripts/dce_backfill.py --start 20200101 --end 20221231
"""
from __future__ import annotations

import os
import sys

# 把技能脚本目录加入搜索路径（不复制 600 行，直接复用）
_SKILL_SCRIPTS = r"C:\Users\Seven\.codebuddy\skills\scrapling-qi\scripts"
if _SKILL_SCRIPTS not in sys.path:
    sys.path.insert(0, _SKILL_SCRIPTS)

import DCE_scrapling_crawler as dc  # noqa: E402

# ---- 仅覆盖连接目标：默认云端（隧道），可用环境变量切到本地 ----
dc.PG = dict(
    host=os.getenv("DCE_PG_HOST", "127.0.0.1"),
    port=int(os.getenv("DCE_PG_PORT", "15432")),
    dbname=os.getenv("DCE_PG_DB", "futures"),
    user=os.getenv("DCE_PG_USER", "futures"),
    password=os.getenv("DCE_PG_PASSWORD", "qhyc_dev_pwd_2026"),
)

# ---- 兼容 2020 年代初 DCE 报表列名（持买量/持卖量，缺「单」字）----
dc.METRIC_MAP.setdefault("持买量", "long")
dc.METRIC_MAP.setdefault("持卖量", "short")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(dc.main())
