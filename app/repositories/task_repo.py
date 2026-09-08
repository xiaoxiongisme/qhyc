"""任务流水仓储"""
from __future__ import annotations

from datetime import datetime
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
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish(self, run: TaskRun, status: str, message: str | None = None) -> None:
        run.status = status
        run.finished_at = datetime.utcnow()
        run.message = message

    def get(self, run_id: int) -> TaskRun | None:
        return self.session.get(TaskRun, run_id)

    def list_recent(self, limit: int = 50, task_type: str | None = None) -> list[TaskRun]:
        stmt = select(TaskRun).order_by(TaskRun.id.desc()).limit(limit)
        if task_type:
            stmt = stmt.where(TaskRun.task_type == task_type)
        return list(self.session.execute(stmt).scalars().all())