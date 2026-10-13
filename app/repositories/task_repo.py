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
        finished = datetime.now(timezone.utc)
        # ⚠ 修复①（时区）：原 datetime.utcnow() 是 naive UTC，而列是 DateTime(timezone=True)，
        #   psycopg 会按会话时区(Asia/Shanghai)解释 naive 值，导致 finished_at 比真实
        #   完成时间早 8 小时 → max(finished_at) 取到错误日期 → 作业心跳巡检误报"停摆"。
        #   改用带时区的 datetime.now(timezone.utc)，入库即正确 UTC，回读一致。
        # ⚠ 修复②（孤儿）：先在当前（通常为工作）会话上提交终态；若该会话已处于"无效事务"
        #   状态（如长事务中连接被 DB 重置——见 backtest 2026-10-11
        #   "Can't reconnect until invalid transaction is rolled back"），commit 会失败且
        #   run 记录会永远卡 running 成孤儿，使心跳巡检误报"停摆"且掩盖真故障。此时降级用
        #   一条全新会话把终态落库，确保作业记录不丢、不被孤儿记录误导。
        try:
            run.status = status
            run.finished_at = finished
            run.message = message
            self.session.commit()
        except Exception:
            self._record_terminal_fresh(run, status, message, finished)

    def _record_terminal_fresh(
        self, run: TaskRun, status: str, message: str | None, finished
    ) -> None:
        """兜底：当前会话事务已失效时，用独立新会话把 run 终态落库。"""
        from app.core.db import session_scope

        try:
            with session_scope() as s:
                db_run = s.get(TaskRun, run.id)
                if db_run is None:
                    db_run = TaskRun(
                        id=run.id,
                        task_type=run.task_type,
                        label=run.label,
                        started_at=run.started_at,
                    )
                    s.add(db_run)
                db_run.status = status
                db_run.finished_at = finished
                db_run.message = message
        except Exception as exc:  # noqa: BLE001 —— 连兜底都失败只记日志，绝不抛回掩盖真实作业错误
            import logging

            logging.getLogger(__name__).exception(
                "[task_repo] finish 降级落库也失败 (run_id=%s): %s", run.id, exc
            )

    def get(self, run_id: int) -> TaskRun | None:
        return self.session.get(TaskRun, run_id)

    def list_recent(self, limit: int = 50, task_type: str | None = None) -> list[TaskRun]:
        stmt = select(TaskRun).order_by(TaskRun.id.desc()).limit(limit)
        if task_type:
            stmt = stmt.where(TaskRun.task_type == task_type)
        return list(self.session.execute(stmt).scalars().all())