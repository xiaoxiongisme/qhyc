#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qhyc 定时任务统一入口（crontab 包装器）——查字典 → 执行 → 记流水。

背景（2026-10-14 用户需求）：所有定时任务登记到数据库字典表
``ops.scheduled_task``，后期通过字典表控制；每次运行的成败记录落
``ops.scheduled_task_run``。本脚本是**宿主 crontab 侧**的执行入口：

    python3 task_runner.py --task-id cron_fetch_1min --shell-cmd 'docker exec ...'

行为（与容器内 app/ops/task_registry.py 语义一致）：
  1. 字典表不存在（迁移 043 未应用）/ 查询失败 → WARNING 后**照常执行**
     （fail-open：记账基础设施故障不得阻断生产任务）；
  2. 字典 enabled=false → 记一条 disabled_skipped 流水，**不执行**，退出 0；
  3. task_id 未登记 → 自动补一行最小字典行（purpose=待补录）+ WARNING，照常执行；
  4. 执行命令，按退出码记 success/failed（含耗时与输出尾部）。

DB 访问走 ``docker exec qhyc-timescaledb psql``（与 ops_monitor.py 同方式）——
宿主无 psycopg2，不引第三方依赖。防注入：task_id 限定 ``[A-Za-z0-9_.-]``，
字符串一律经 psql ``:'var'`` 变量插值（自动正确转义）。

用法：
  # shell 字符串模式（crontab 原生命令，含 flock/重定向）
  python3 task_runner.py --task-id cron_fetch_1min \\
      --shell-cmd 'cd /Docker/qhyc && flock -n /tmp/x.lock docker exec qhyc-api python scripts/x.py'

  # argv 模式（无需 shell 语义时更安全）
  python3 task_runner.py --task-id cron_ops_monitor -- /usr/bin/python3 /opt/qhyc-ops/ops_monitor.py

  # 试运行：只查字典不执行
  python3 task_runner.py --task-id cron_fetch_1min --shell-cmd 'true' --dry-run
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

PSQL = ["docker", "exec", "-i", "qhyc-timescaledb", "psql", "-U", "futures", "-d", "futures",
        "-v", "ON_ERROR_STOP=1", "-t", "-A"]
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _psql_query(sql: str, vars: dict | None = None, timeout: int = 30) -> tuple[bool, str]:
    """执行一条 SQL，返回 (ok, 输出)。

    SQL 经 stdin 传入（``docker exec -i ... psql``，不带 ``-c``）：已实测云端该方式
    下 psql 变量插值（``:'var'``）正常工作，避免 ``-c`` 模式下插值/引号差异导致的语法错。
    """
    cmd = list(PSQL)
    for k, v in (vars or {}).items():
        cmd += ["-v", f"{k}={v}"]
    try:
        p = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 —— docker 不在/超时都算不可用
        return False, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip()
    return True, (p.stdout or "").strip()


def _dict_enabled(task_id: str) -> tuple[bool, str]:
    """返回 (放行与否, 状态说明)。查询失败 → 放行（fail-open）。"""
    ok, out = _psql_query(
        "SELECT enabled::text FROM ops.scheduled_task WHERE task_id = :'task_id'",
        {"task_id": task_id},
    )
    if not ok:
        # 表不存在（迁移未应用）与库不可用是两种情形，但都按 fail-open 处理
        return True, f"dict_unreadable({out[:120]})"
    if out == "":
        # 未登记：补最小字典行留痕，再放行
        ok2, err2 = _psql_query(
            "INSERT INTO ops.scheduled_task (task_id, name, purpose, executor, schedule) "
            "VALUES (:'task_id', :'task_id', '待补录：由 task_runner 自动创建，"
            "请补全 name/purpose/schedule', 'manual', 'unknown') "
            "ON CONFLICT (task_id) DO NOTHING",
            {"task_id": task_id},
        )
        if not ok2:
            print(f"[task_runner] WARNING 自动补登记失败: {err2[:200]}", file=sys.stderr)
        else:
            print(f"[task_runner] WARNING task_id={task_id} 未登记，已自动补最小字典行（待补录）",
                  file=sys.stderr)
        return True, "unregistered_autocreated"
    if out in ("t", "true", "1"):
        return True, "enabled"
    return False, "disabled"


def _record_run(task_id: str, status: str, started_at: datetime, duration_s: float,
                exit_code: int | None, message: str) -> None:
    """写流水。失败只 WARNING——记账不得反过来搞挂任务。"""
    msg = (message or "").replace("'", "''")[:500]
    ok, err = _psql_query(
        "INSERT INTO ops.scheduled_task_run "
        "(task_id, status, trigger, started_at, finished_at, duration_s, exit_code, message) "
        "VALUES (:'task_id', :'status', 'auto', :'started_at', now(), :duration_s, "
        "        :exit_code, :'message')",
        {
            "task_id": task_id,
            "status": status,
            "started_at": started_at.isoformat(),
            "duration_s": f"{duration_s:.3f}",
            "exit_code": "" if exit_code is None else str(exit_code),
            "message": msg,
        },
    )
    if not ok:
        print(f"[task_runner] WARNING 流水记录失败: {err[:200]}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="qhyc 定时任务统一入口（查字典→执行→记流水）")
    ap.add_argument("--task-id", required=True, help="字典任务 id（须登记于 ops.scheduled_task）")
    ap.add_argument("--shell-cmd", dest="shell_cmd", default=None,
                    help="以 bash -c 执行的完整命令串（crontab 原生命掷含 flock/重定向时用）")
    ap.add_argument("--dry-run", action="store_true", help="只查字典与试跑判定，不执行")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- 之后的 argv（与 --shell-cmd 二选一）")
    args = ap.parse_args()

    task_id = args.task_id
    if not TASK_ID_RE.match(task_id):
        print(f"[task_runner] ERROR 非法 task_id: {task_id!r}", file=sys.stderr)
        return 2

    argv = [c for c in args.cmd if c != "--"]
    if not args.shell_cmd and not argv:
        print("[task_runner] ERROR 未提供执行命令（--shell-cmd 或 -- argv）", file=sys.stderr)
        return 2

    allowed, why = _dict_enabled(task_id)
    if not allowed:
        _record_run(task_id, "disabled_skipped", datetime.now(timezone.utc), 0.0, None,
                    f"字典禁用（{why}），未执行")
        print(f"[task_runner] {task_id} 被字典禁用（ops.scheduled_task.enabled=false），跳过执行")
        return 0

    if args.dry_run:
        print(f"[task_runner] dry-run: {task_id} 放行（{why}）")
        return 0

    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    if args.shell_cmd:
        proc = subprocess.run(["bash", "-c", args.shell_cmd])
        rc, out_tail = proc.returncode, ""
    else:
        try:
            proc = subprocess.run(argv)
            rc, out_tail = proc.returncode, ""
        except FileNotFoundError as e:
            rc, out_tail = 127, f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            rc, out_tail = -1, f"{type(e).__name__}: {e}"
    duration = time.monotonic() - t0

    status = "success" if rc == 0 else "failed"
    msg = out_tail or f"exit_code={rc}"
    _record_run(task_id, status, started, duration, rc, msg)
    print(f"[task_runner] {task_id} {status} rc={rc} duration={duration:.1f}s")
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())
