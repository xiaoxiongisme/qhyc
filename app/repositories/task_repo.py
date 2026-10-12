"""任务流水仓储"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaskRun


class TaskRepository:
    def __init__(self, session: Session):
        self.session = session

    def start(self, task_type: str, label: str | None = None, payload: dict | None = None) -> TaskRun:
        run = TaskRun(
            task_type=task_type,
            label=label,
            status="running",
            payload=payload or {},
            # 显式写入带时区的 UTC 时间戳，避免依赖 DB server_default；与 finish 一致。
            started_at=datetime.now(timezone.utc),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish(self, run: TaskRun, status: str, message: str | None = None) -> None:
        run.status = status
        # ⚠ 修复：原 datetime.utcnow() 是 naive UTC，而列是 DateTime(timezone=True)，
        #   psycopg 会按会话时区(Asia/Shanghai)解释 naive 值，导致 finished_at 比真实
        #   完成时间早 8 小时 → max(finished_at) 取到错误日期 → 作业心跳巡检误报"停摆"。
        #   改用带时区的 datetime.now(timezone.utc)，入库即正确 UTC，回读一致。
        run.finished_at = datetime.now(timezone.utc)
        run.message = message

    def get(self, run_id: int) -> TaskRun | None:
        return self.session.get(TaskRun, run_id)

    def list_recent(self, limit: int = 50, task_type: str | None = None) -> list[TaskRun]:
        stmt = select(TaskRun).order_by(TaskRun.id.desc()).limit(limit)
        if task_type:
            stmt = stmt.where(TaskRun.task_type == task_type)
        return list(self.session.execute(stmt).scalars().all())