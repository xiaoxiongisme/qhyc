"""运行留痕：`pipeline_run` / `pipeline_push_log`（PRD §7）。

幂等规则：`daily` 同一 `run_date` 重跑 → **更新同一行**，不新增（验收第 7 条）。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select

from app.core.db import session_scope
from app.core.logging import logger
from app.models import PipelinePushLog, PipelineRun


def start_run(kind: str, run_date: date | None = None,
              manifest: str | None = None) -> int:
    """开跑登记。daily 复用同日行；其余 kind 每次新建。"""
    run_date = run_date or date.today()
    with session_scope() as s:
        row = None
        if kind == "daily":
            row = s.execute(
                select(PipelineRun).where(
                    PipelineRun.kind == kind, PipelineRun.run_date == run_date
                )
            ).scalar_one_or_none()
        if row is None:
            row = PipelineRun(kind=kind, run_date=run_date, status="running")
        else:
            row.status = "running"
            row.error = None
            row.finished_at = None
        row.src_manifest = manifest
        row.created_by = "scheduler"
        s.add(row)
        s.flush()
        rid = int(row.run_id)
    logger.info(f"[pipeline] start run id={rid} kind={kind} date={run_date}")
    return rid


def finish_run(run_id: int, status: str, *, steps: list[dict] | None = None,
               artifacts: list[str] | None = None, data_ready: bool | None = None,
               error: str | None = None) -> None:
    with session_scope() as s:
        row = s.get(PipelineRun, run_id)
        if row is None:
            return
        row.status = status
        row.steps = steps or row.steps
        row.artifacts = artifacts or row.artifacts
        if data_ready is not None:
            row.data_ready = data_ready
        row.error = (error or None)
        s.add(row)
    logger.info(f"[pipeline] finish run id={run_id} status={status} err={error}")


def log_push(run_id: int, channel: str, ok: bool, title: str, resp: str) -> None:
    with session_scope() as s:
        s.add(PipelinePushLog(run_id=run_id, channel=channel, ok=ok,
                              title=title, resp=(resp or "")[:2000]))


def latest(kind: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(
            select(PipelineRun)
            .where(PipelineRun.kind == kind)
            .order_by(PipelineRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _as_dict(row) if row else None


def list_runs(kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
        if kind:
            q = q.where(PipelineRun.kind == kind)
        return [_as_dict(r) for r in s.execute(q).scalars().all()]


def get_run(run_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.get(PipelineRun, run_id)
        return _as_dict(row) if row else None


def ensure_tables() -> None:
    """运行期兜底建表（db/init 只在空数据卷首次初始化时跑）。幂等、失败不阻断。"""
    try:
        from app.core.db import get_engine
        from app.models.base import Base

        Base.metadata.create_all(
            get_engine(), tables=[PipelineRun.__table__, PipelinePushLog.__table__]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pipeline] ensure_tables 跳过: {e}")


def _as_dict(row: PipelineRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "kind": row.kind,
        "run_date": row.run_date.isoformat() if row.run_date else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "status": row.status,
        "steps": row.steps,
        "artifacts": row.artifacts,
        "src_manifest": row.src_manifest,
        "data_ready": row.data_ready,
        "error": row.error,
        "created_by": row.created_by,
    }
