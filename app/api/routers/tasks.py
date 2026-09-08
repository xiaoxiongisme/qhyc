"""/tasks - 任务流水"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.models import TaskRun
from app.repositories.task_repo import TaskRepository
from app.schemas.task import TaskRunOut

router = APIRouter()


@router.get("", response_model=list[TaskRunOut])
def list_tasks(
    task_type: str | None = None,
    limit: int = 50,
    db: Session = Depends(fastapi_db_dep),
):
    rows = TaskRepository(db).list_recent(limit=limit, task_type=task_type)
    return [_to_out(r) for r in rows]


@router.get("/{task_id}", response_model=TaskRunOut)
def get_task(task_id: int, db: Session = Depends(fastapi_db_dep)):
    r = TaskRepository(db).get(task_id)
    if not r:
        raise HTTPException(404, f"task {task_id} not found")
    return _to_out(r)


def _to_out(r: TaskRun) -> TaskRunOut:
    return TaskRunOut(
        id=r.id,
        task_type=r.task_type,
        label=r.label,
        status=r.status,
        started_at=r.started_at,
        finished_at=r.finished_at,
        message=r.message,
        payload=r.payload,
    )