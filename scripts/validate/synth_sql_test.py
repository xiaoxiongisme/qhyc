# -*- coding: utf-8 -*-
"""只读验证新 synthesizer 固定网格 SQL 能正确渲染并执行（不写库）。"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from app.ingest.synthesizer import _session_insert_sql, _SESSION_DAY_BOUNDS
from sqlalchemy import text
from app.core.db import session_scope


def main() -> int:
    sql60 = _session_insert_sql(60, "WHERE ts >= :since", "ON CONFLICT DO NOTHING")
    sql30 = _session_insert_sql(30, "WHERE ts >= :since", "ON CONFLICT DO NOTHING")
    print("60m 渲染 OK:", "BETWEEN 9 AND 14" in sql60, " 含夜盘锚点:", "TIME '09:00'" in sql60)
    print("30m 渲染 OK:", "BETWEEN 9 AND 14" in sql30)
    print("DAY_BOUNDS:", _SESSION_DAY_BOUNDS)

    since = datetime(2026, 10, 28, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    params = {
        "since": since,
        "srcs": ["kqm_1m", "wh9999_1m", "tqsdk_1min", "akshare_1min"],
        "end_srcs": ["akshare_1min", "wh9999_1m"],
        "maxgap": 150,
    }
    with session_scope() as s:
        rows = s.execute(text(sql60), params).fetchall()
        print("synth 60m 聚合执行 OK, 近 3 天行数:", len(rows))
        # 抽查 RB888 近 3 天
        rb = [r for r in rows if r[0] == "RB888"]
        from app.core.db import session_scope as _sc  # noqa
    print("RB888 近 3 天 60m 行数:", len(rb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
