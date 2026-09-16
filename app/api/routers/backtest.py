"""/backtest - M4 回测（PRD §6）"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.api.routers.health import _readiness
from app.backtest.engine import BacktestParams, backtest_symbols
from app.core.db import session_scope
from app.core.logging import logger
from app.repositories.task_repo import TaskRepository

router = APIRouter()


class BacktestRequest(BaseModel):
    symbols: Optional[list[str]] = Field(None, description="缺省=全部主连品种")
    test_days: int = Field(60, ge=20, le=250, description="回测覆盖的最近交易日数")
    step: int = Field(3, ge=1, le=20, description="评估点间隔（根）")
    async_run: bool = True


@router.post("")
def run_backtest(req: BacktestRequest, bg: BackgroundTasks):
    """执行回测（PRD §6）：逐评估点预测 vs 真实，per-model 指标 + Hurst 分层"""
    rd = _readiness()
    if not rd.get("ready"):
        raise HTTPException(503, detail={"message": "数据未就绪，拒绝回测", "readiness": rd})

    def _task():
        try:
            with session_scope() as s:
                repo = TaskRepository(s)
                run = repo.start(
                    "backtest", label="manual",
                    payload={k: v for k, v in req.model_dump().items() if v is not None},
                )
                params = BacktestParams(test_days=req.test_days, step=req.step)
                out = backtest_symbols(s, symbols=req.symbols, params=params)
                ok = sum(1 for r in out if "error" not in r and "skipped" not in r)
                repo.finish(run, "success", f"{ok}/{len(out)} symbols")
                logger.info(f"[backtest] done {ok}/{len(out)}")
        except Exception as e:
            logger.exception(f"[backtest] failed: {e}")

    if req.async_run:
        bg.add_task(_task)
        return {"status": "scheduled", "message": "backtest 在后台执行（完成后查 /backtests）"}

    _task()
    return {"status": "done"}


@router.get("")
def list_backtests(limit: int = 20, run_id: str | None = None):
    """回测结果列表（最新 run 或指定 run）"""
    from sqlalchemy import select
    from app.models import BacktestResult
    from app.core.db import get_engine

    with get_engine().connect() as conn:
        if run_id:
            rows = conn.execute(
                select(BacktestResult).where(BacktestResult.run_id == run_id)
            ).scalars().all()
        else:
            latest = conn.execute(
                select(BacktestResult.run_id).order_by(BacktestResult.created_at.desc()).limit(1)
            ).scalar()
            if not latest:
                return {"run_id": None, "results": []}
            rows = conn.execute(
                select(BacktestResult).where(BacktestResult.run_id == latest)
            ).scalars().all()
            run_id = latest

    from sqlalchemy.orm import Session as _S

    out = []
    for r in rows:
        bs = r.by_state or {}
        out.append(
            {
                "run_id": r.run_id,
                "model": r.model,
                "window": r.window,
                "start_date": r.start_date.isoformat() if r.start_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
                "dir_acc": float(r.dir_acc) if r.dir_acc is not None else None,
                "mae": float(r.mae) if r.mae is not None else None,
                "rmse": float(r.rmse) if r.rmse is not None else None,
                "quantile_hit": float(r.quantile_hit) if r.quantile_hit is not None else None,
                "sample_n": r.sample_n,
                "recent60_dir_acc": bs.get("recent60_dir_acc"),
                "by_state": bs.get("states"),
                "gate_dist": bs.get("gate_dist"),
                "source": bs.get("source"),
                # M5 看板标注依据（审计 P1-3 遗留）：区间未校准的模型需显式提示
                "interval_calibrated": bs.get(
                    "interval_calibrated", r.model not in ("rf", "xgb", "wavelet")
                ),
                "caliber": getattr(r, "caliber", "close"),  # §17：口径声明
            }
        )
    return {"run_id": run_id, "count": len(out), "results": sorted(out, key=lambda x: -(x["dir_acc"] or 0))}


@router.post("/update-weights")
def update_weights():
    """⑳ 手动触发权重月更（按最新回测近 60 日准确率）"""
    from app.backtest.weights import update_model_weights

    with session_scope() as s:
        res = update_model_weights(s)
    return res