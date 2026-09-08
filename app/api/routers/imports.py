"""/imports - 本地历史数据导入"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep, session_scope
from app.core.logging import logger
from app.ingest.local_importer import LocalHistoryImporter
from app.repositories.task_repo import TaskRepository

router = APIRouter()


class ImportRequest(BaseModel):
    product: str | None = None   # None=全部
    async_run: bool = True


@router.post("/local")
def import_local(req: ImportRequest, bg: BackgroundTasks):
    """按 §4.5 模板导入用户本地历史数据（FG/SA 等）"""

    def _task():
        try:
            with session_scope() as s:
                repo = TaskRepository(s)
                run = repo.start("import", label="local", payload=req.model_dump())
                task_id = run.id
                imp = LocalHistoryImporter(s)
                results = (
                    [imp.import_product(req.product)]
                    if req.product
                    else imp.import_all()
                )
                repo.finish(run, "success", str(results)[:500])
                logger.info(f"[import] local done task_id={task_id}")
        except Exception as e:
            logger.exception(f"[import] local failed: {e}")

    if req.async_run:
        bg.add_task(_task)
        return {"status": "scheduled", "scope": req.product or "all"}

    _task()
    return {"status": "done"}


@router.get("/discover")
def discover(db: Session = Depends(fastapi_db_dep)):
    """扫描可导入的品种（容器内 /app/imports 下的子目录）"""
    imp = LocalHistoryImporter(db)
    return {"products": imp.discover_products()}