"""/ingest - 手动触发更新"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import fastapi_db_dep, session_scope
from app.core.logging import logger
from app.ingest.orchestrator import IngestOrchestrator
from app.repositories.task_repo import TaskRepository
from app.schemas.ingest import IngestReportOut

router = APIRouter()


class IngestRequest(BaseModel):
    symbol: Optional[str] = Field(None, description="指定品种；为空表示全量")
    start: Optional[date] = None
    end: Optional[date] = None
    skip_calibration: bool = False
    predict: bool = Field(True, description="⑦ 入库完成后联动预测")
    async_run: bool = Field(False, description="True=后台执行，立即返回 task_id")


@router.post("", response_model=IngestReportOut | dict)
def trigger_ingest(req: IngestRequest, bg: BackgroundTasks):
    """手动触发一次采集（§7）"""
    settings = get_settings()

    def _task():
        try:
            with session_scope() as s:
                repo = TaskRepository(s)
                run = repo.start("ingest", label="manual", payload=req.model_dump())
                task_id = run.id

                orch = IngestOrchestrator(s)
                if req.symbol:
                    spec = next(
                        (x for x in settings.main_contracts if x.symbol == req.symbol),
                        None,
                    )
                    if not spec:
                        repo.finish(run, "failed", f"unknown symbol {req.symbol}")
                        return
                    reports = [orch.ingest_symbol(spec, req.start, req.end, req.skip_calibration)]
                else:
                    reports = orch.ingest_all(req.start, req.end)

                # M2.1 平滑主连自动延伸（R1）：先延伸再预测
                try:
                    from app.ingest.smooth_extender import extend_all

                    extend_all(s)
                except Exception as ee:
                    logger.warning(f"[ingest] smooth extend failed: {ee}")

                # ⑦ 预测联动：对本次成功入库的品种触发预测
                if req.predict:
                    done_syms = [r.symbol for r in reports if not r.error]
                    if done_syms:
                        from app.engine.service import predict_symbols

                        prepo = TaskRepository(s)
                        prun = prepo.start("predict", label="after-manual-ingest", payload={"symbols": done_syms})
                        try:
                            out = predict_symbols(s, symbols=done_syms)
                            ok = sum(1 for r in out if "error" not in r)
                            prepo.finish(prun, "success", f"{ok}/{len(out)} symbols")
                        except Exception as pe:
                            prepo.finish(prun, "failed", str(pe))
                            logger.warning(f"[ingest] 联动预测失败: {pe}")

                # 汇总
                summary = {
                    "symbols": [r.to_dict() for r in reports],
                    "finished_at": datetime.utcnow().isoformat() + "Z",
                }
                failed = [r for r in reports if r.error]
                status = "failed" if failed and not req.skip_calibration else "success"
                repo.finish(run, status, message=str(summary)[:500])
                logger.info(f"[ingest] manual done task_id={task_id} status={status}")
        except Exception as e:
            logger.exception(f"[ingest] manual task failed: {e}")

    if req.async_run:
        bg.add_task(_task)
        return {"status": "scheduled", "message": "ingest 已放入后台任务"}

    # 同步：等执行完毕
    _task()
    # 取最新一条任务报告
    with session_scope() as s:
        last = TaskRepository(s).list_recent(limit=1, task_type="ingest")
        if not last:
            return {"status": "unknown"}
        run = last[0]
        return IngestReportOut(
            task_id=run.id,
            task_type=run.task_type,
            label=run.label,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            message=run.message,
        ).model_dump()


@router.get("/progress")
def ingest_progress(db: Session = Depends(fastapi_db_dep)):
    """R3② 回补进度：目标品种清单 × 各品种入库进度（行数 + 最新日期）"""
    from sqlalchemy import text

    settings = get_settings()
    rows = db.execute(
        text(
            """
            SELECT symbol, count(*) AS n, max(trade_date) AS maxd
            FROM daily_bar
            WHERE symbol LIKE '%888'
            GROUP BY symbol
            """
        )
    ).all()
    data_map = {r[0]: {"rows": int(r[1]), "latest": r[2].isoformat() if r[2] else None} for r in rows}

    targets = [
        {
            "symbol": s.symbol,
            "name": s.name,
            "exchange": s.exchange,
            "ingested": s.symbol in data_map,
            "rows": data_map.get(s.symbol, {}).get("rows", 0),
            "latest": data_map.get(s.symbol, {}).get("latest"),
        }
        for s in settings.main_contracts
    ]
    done = sum(1 for t in targets if t["ingested"])
    return {
        "target_total": len(targets),
        "done": done,
        "progress": f"{done}/{len(targets)}",
        "ready": done >= len(targets),
        "remaining": [t["symbol"] for t in targets if not t["ingested"]],
        "symbols": targets,
    }


@router.post("/symbol/{symbol}", response_model=dict)
def trigger_symbol(symbol: str, start: date | None = None, end: date | None = None):
    """便捷：单品种更新"""
    settings = get_settings()
    with session_scope() as s:
        repo = TaskRepository(s)
        run = repo.start("ingest", label=f"manual-{symbol}", payload={"symbol": symbol})
        orch = IngestOrchestrator(s)
        spec = next((x for x in settings.main_contracts if x.symbol == symbol), None)
        if not spec:
            repo.finish(run, "failed", f"unknown symbol {symbol}")
            return {"status": "failed", "message": f"unknown symbol {symbol}"}
        rep = orch.ingest_symbol(spec, start, end)
        repo.finish(run, "success" if not rep.error else "failed", str(rep.to_dict())[:500])
        return {"status": "success", "report": rep.to_dict()}