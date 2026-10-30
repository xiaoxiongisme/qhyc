# -*- coding: utf-8 -*-
"""软禁用 cron_fetch_1min —— 它引用的脚本已于 63163ce 删除，任务在空转报错。

处置选择（为什么不是直接删 crontab）：
  1. 可回滚：ops.scheduled_task.enabled=false → task_runner 记 disabled_skipped
     流水、不执行、退出 0（scripts/ops/task_runner.py:14）。恢复只需改回 true。
  2. 保留现场：crontab 行不动，将来若 1 分钟补采脚本回归，开关一拉即可。
  3. 比删 crontab 更符合「手能缩回去」原则。
"""
import sys

from sqlalchemy import text
from app.core.db import session_scope

TASK_ID = "cron_fetch_1min"
REASON = ("2026-10-31 停用：引用的 scripts/fetch_contract_1min.py 已于 63163ce "
          "(2026-10-09) 删除，任务每 15 分钟报 No such file or directory 空转。"
          "恢复前须先落地替代采集脚本。")

apply = "--apply" in sys.argv

with session_scope() as s:
    row = s.execute(text("""
        SELECT task_id, enabled, COALESCE(name,'') AS name, COALESCE(purpose,'') AS purpose
        FROM ops.scheduled_task WHERE task_id = :t
    """), {"t": TASK_ID}).first()

    if row is None:
        print(f"[!] 未找到 {TASK_ID}，无需处理")
        sys.exit(0)

    print("=" * 74)
    print(f"处置对象: {TASK_ID}")
    print(f"  当前 enabled : {row[1]}")
    print(f"  name         : {row[2]}")
    print(f"  purpose      : {row[3]}")
    print("=" * 74)

    if not apply:
        print("\n[DRY-RUN] 未做任何改动。加 --apply 执行软禁用。")
        print(f"将执行：UPDATE ops.scheduled_task SET enabled=false, ")
        print(f"        name=..., purpose=... WHERE task_id='{TASK_ID}'")
        sys.exit(0)

    before = (row[1], row[2], row[3])
    s.execute(text("""
        UPDATE ops.scheduled_task
        SET enabled = false,
            name    = :nm,
            purpose = :pp,
            updated_at = now()
        WHERE task_id = :t
    """), {"t": TASK_ID,
           "nm": row[2] + "【已停用 2026-10-31：脚本已删除】",
           "pp": row[3] + " || " + REASON})

    rows = s.execute(text("""
        SELECT count(*) FROM ops.scheduled_task WHERE task_id = :t AND enabled = false
    """), {"t": TASK_ID}).fetchall()
    print(f"\n[✓] 已软禁用，校验 enabled=false 行数={rows[0][0]}")
    print(f"    回滚命令（如需恢复）：")
    print(f"    UPDATE ops.scheduled_task SET enabled=true WHERE task_id='{TASK_ID}';")
    print(f"    变更前原值: enabled={before[0]}")
