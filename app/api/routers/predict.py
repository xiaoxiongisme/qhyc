"""/predict - M2 预测服务（§5.4，含 R3① 就绪门控）"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routers.health import _readiness
from app.core.db import fastapi_db_dep, session_scope
from app.core.logging import logger
from app.engine.service import predict_symbol, predict_symbols
from app.models import PredictionResult
from app.repositories.task_repo import TaskRepository

router = APIRouter()


def _gate():
    """R3① 就绪门控：数据未就绪 → 503，绝不静默计算"""
    rd = _readiness()
    if not rd.get("ready"):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "数据尚未就绪，拒绝预测（避免基于不完整数据静默算错）",
                "readiness": rd,
            },
        )
    return rd


class PredictRequest(BaseModel):
    symbol: str = Field(..., examples=["FG888"])
    as_of_date: Optional[date] = Field(None, description="缺省=该品种最新数据日")
    save: bool = True


class BatchPredictRequest(BaseModel):
    symbols: Optional[list[str]] = Field(None, description="缺省=全部主连品种")
    as_of_date: Optional[date] = None
    async_run: bool = False


@router.post("")
def predict(req: PredictRequest):
    """单品种预测：返回 §5.3 JSON 契约 v1（+ §16.6 drivers 预留字段）"""
    _gate()
    with session_scope() as s:
        # §16.1 金融期货默认不纳入预测范围
        from sqlalchemy import select as _select
        from app.models import SectorMap

        tprod = req.symbol[:-3] if req.symbol.upper().endswith("888") else req.symbol
        active = s.execute(
            _select(SectorMap.active).where(SectorMap.product == tprod.upper())
        ).scalar()
        if active is False:
            raise HTTPException(
                400, f"{req.symbol} 属金融期货（§16.1 默认不纳入预测范围，配置开关可开）"
            )
        repo = TaskRepository(s)
        run = repo.start("predict", label="single", payload=req.model_dump(mode="json"))
        try:
            out = predict_symbol(s, req.symbol, as_of_date=req.as_of_date, save=req.save)
            repo.finish(run, "success", f"{req.symbol} {out['direction']} p={out['direction_prob']}")
            return out
        except ValueError as e:
            repo.finish(run, "failed", str(e))
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            repo.finish(run, "failed", str(e))
            logger.exception(f"[predict] {req.symbol} 失败")
            raise HTTPException(500, str(e)) from e


@router.post("/batch")
def predict_batch(req: BatchPredictRequest, bg: BackgroundTasks):
    """批量预测全部/指定品种（看板热力图）"""
    _gate()

    if req.async_run:

        def _task():
            try:
                with session_scope() as s:
                    repo = TaskRepository(s)
                    run = repo.start(
                        "predict", label="batch-async", payload=req.model_dump(mode="json")
                    )
                    out = predict_symbols(s, symbols=req.symbols, as_of_date=req.as_of_date)
                    ok = sum(1 for r in out if "error" not in r)
                    repo.finish(run, "success", f"{ok}/{len(out)} symbols")
            except Exception as e:
                logger.exception(f"[predict] batch async failed: {e}")

        bg.add_task(_task)
        return {"status": "scheduled", "message": "batch prediction 在后台执行"}

    with session_scope() as s:
        repo = TaskRepository(s)
        run = repo.start("predict", label="batch", payload=req.model_dump(mode="json"))
        try:
            out = predict_symbols(s, symbols=req.symbols, as_of_date=req.as_of_date)
            ok = sum(1 for r in out if "error" not in r)
            repo.finish(run, "success", f"{ok}/{len(out)} symbols")
            return {"total": len(out), "ok": ok, "results": out}
        except Exception as e:
            repo.finish(run, "failed", str(e))
            raise HTTPException(500, str(e)) from e


@router.get("")
def list_predictions(
    symbol: Optional[str] = None,
    target_date: Optional[date] = None,
    limit: int = 50,
    db: Session = Depends(fastapi_db_dep),
):
    """查询历史预测（同 as_of 多 run 可追溯）"""
    stmt = select(PredictionResult).order_by(PredictionResult.as_of_ts.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(PredictionResult.symbol == symbol)
    if target_date:
        stmt = stmt.where(PredictionResult.target_date == target_date)
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "run_id": r.run_id,
            "symbol": r.symbol,
            "target_date": r.target_date.isoformat(),
            "as_of_date": r.as_of_date.isoformat(),
            "as_of_ts": r.as_of_ts.isoformat(),
            "model_set": r.model_set,
            "direction": r.direction,
            "direction_prob": float(r.direction_prob) if r.direction_prob is not None else None,
            "ret_point": float(r.ret_point) if r.ret_point is not None else None,
            "ret_low": float(r.ret_low) if r.ret_low is not None else None,
            "ret_high": float(r.ret_high) if r.ret_high is not None else None,
            "confidence": float(r.confidence) if r.confidence is not None else None,
            "state": r.state,
            "participated_models": r.participated_models,
        }
        for r in rows
    ]
