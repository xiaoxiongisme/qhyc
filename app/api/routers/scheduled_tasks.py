# -*- coding: utf-8 -*-
"""/ops/tasks - 定时任务字典与运行流水查询（2026-10-14，迁移 043）。

用户需求：所有定时任务（DB 内作业/crontab/APScheduler/systemd）与存储过程
登记到 ops.scheduled_task，运行成败记录落 ops.scheduled_task_run，
"后期好查询"。本路由提供三个查询入口：

  GET /ops/tasks            字典清单（可按 executor/enabled 过滤）
  GET /ops/tasks/status     每任务最近一次运行 + 心跳判定 + 24h/7d 成败计数
  GET /ops/tasks/{id}/runs  某任务运行流水（倒序）

SQL 直查（text()）而非 ORM：ops 两张表是运维登记表，无领域模型语义。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep

router = APIRouter()


@router.get("")
def list_tasks(
    executor: str | None = Query(None, description="apscheduler/crontab/systemd/timescaledb_job/stored_proc/manual"),
    enabled: bool | None = Query(None),
    db: Session = Depends(fastapi_db_dep),
):
    rows = db.execute(
        text(
            "SELECT task_id, name, purpose, executor, schedule, schedule_desc, target, "
            "location, enabled, expected_gap_days, owner, tags, updated_at "
            "FROM ops.scheduled_task "
            "WHERE (:executor IS NULL OR executor = :executor) "
            "  AND (:enabled IS NULL OR enabled = :enabled) "
            "ORDER BY executor, task_id"
        ),
        {"executor": executor, "enabled": enabled},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/status")
def task_status(db: Session = Depends(fastapi_db_dep)):
    """每任务：最近一次运行 + 心跳判定 + 近 24h/7d 成败计数。"""
    rows = db.execute(text("SELECT * FROM ops.v_scheduled_task_status ORDER BY task_id")).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{task_id}/runs")
def task_runs(
    task_id: str,
    limit: int = Query(50, ge=1, le=500),
    status: str | None = Query(None),
    db: Session = Depends(fastapi_db_dep),
):
    exists = db.execute(
        text("SELECT 1 FROM ops.scheduled_task WHERE task_id = :id"), {"id": task_id}
    ).scalar()
    if not exists:
        raise HTTPException(404, f"task {task_id} not found in ops.scheduled_task")
    rows = db.execute(
        text(
            "SELECT id, task_id, status, trigger, started_at, finished_at, duration_s, "
            "exit_code, message, host "
            "FROM ops.scheduled_task_run "
            "WHERE task_id = :id AND (:status IS NULL OR status = :status) "
            "ORDER BY started_at DESC LIMIT :limit"
        ),
        {"id": task_id, "status": status, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]
