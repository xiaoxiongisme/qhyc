"""任务流水仓储"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaskRun

logger = logging.getLogger(__name__)


class TaskRepository:
    def __init__(self, session: Session):
        self.session = session
        # 2026-10-23：start 时快照 run 的身份字段。finish 的兜底路径若再去读 run.*，
        # 遇到"会话已废"（异常后未 rollback）会二次抛 PendingRollbackError，终态彻底
        # 丢失、记录永远停在 running——predict 10-21~10-23 三条卡 running 正是如此。
        self._last_run_id: int | None = None
        self._last_run_meta: dict | None = None

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
        self._last_run_id = run.id
        self._last_run_meta = {
            "task_type": run.task_type,
            "label": run.label,
            "started_at": run.started_at,
        }
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
        # ⚠ 修复③（2026-10-23）：兜底路径**绝不碰 run 对象**。原实现在兜底里读
        #   run.id / run.task_type，而这些属性在已废会话上会触发过期加载 → 二次抛
        #   PendingRollbackError → 连兜底都静默失败。改为只用 start 时快照的本原值。
        try:
            run_id = run.id
        except Exception:  # noqa: BLE001 —— 对象已脱离可用会话，退回快照值
            run_id = self._last_run_id
        try:
            run.status = status
            run.finished_at = finished
            run.message = message
            self.session.commit()
            return
        except Exception:
            pass
        self._record_terminal_fresh(run_id, status, message, finished)

    def _record_terminal_fresh(
        self, run_id: int | None, status: str, message: str | None, finished
    ) -> None:
        """兜底：当前会话事务已失效时，用独立新会话把 run 终态落库。"""
        if run_id is None:
            logger.exception("[task_repo] finish 兜底无 run_id（start 快照也缺失），终态丢失")
            return
        try:
            from app.core.db import session_scope

            with session_scope() as s:
                db_run = s.get(TaskRun, run_id)
                if db_run is None:
                    # 极端情况：start 的 INSERT 也随废事务回滚了 → 用快照身份重建
                    meta = self._last_run_meta or {}
                    db_run = TaskRun(
                        id=run_id,
                        task_type=meta.get("task_type") or "unknown",
                        label=meta.get("label"),
                        started_at=meta.get("started_at") or finished,
                    )
                    s.add(db_run)
                db_run.status = status
                db_run.finished_at = finished
                db_run.message = message
        except Exception as exc:  # noqa: BLE001 —— 连兜底都失败只记日志，绝不抛回掩盖真实作业错误
            logger.exception("[task_repo] finish 降级落库也失败 (run_id=%s): %s", run_id, exc)

    def get(self, run_id: int) -> TaskRun | None:
        return self.session.get(TaskRun, run_id)

    def list_recent(self, limit: int = 50, task_type: str | None = None) -> list[TaskRun]:
        stmt = select(TaskRun).order_by(TaskRun.id.desc()).limit(limit)
        if task_type:
            stmt = stmt.where(TaskRun.task_type == task_type)
        return list(self.session.execute(stmt).scalars().all())