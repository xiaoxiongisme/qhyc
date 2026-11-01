# -*- coding: utf-8 -*-
"""查看 ops.scheduled_task 注册表，并给出 cron_fetch_1min 的处置选项。"""
from sqlalchemy import text
from app.core.db import session_scope

SEP = "=" * 78

with session_scope() as s:
    print(SEP)
    print("ops.scheduled_task 当前注册状态")
    print(SEP)
    try:
        cols = s.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='ops' AND table_name='scheduled_task'
            ORDER BY ordinal_position
        """)).fetchall()
        print("  列:", ", ".join(c[0] for c in cols))
        print()
        rows = s.execute(text("""
            SELECT task_id, enabled, COALESCE(purpose,'') AS purpose,
                   COALESCE(name,'') AS name
            FROM ops.scheduled_task ORDER BY task_id
        """)).fetchall()
        print(f"  {'task_id':<26}{'enabled':>9}  {'name':<20}用途")
        print("  " + "-" * 78)
        for r in rows:
            print(f"  {r[0]:<26}{str(r[1]):>9}  {r[2][:18]:<20}{(r[3] or '')[:34]}")
    except Exception as e:
        print("  ERR:", str(e).replace("\n", " ")[:200])

    print()
    print(SEP)
    print("cron_fetch_1min 的判定依据：被它引用的脚本是否存在于仓库")
    print(SEP)
