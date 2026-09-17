"""/backtest - M4 回测（PRD §6）"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.api.routers.health import _readiness
from app.backtest.engine import BacktestParams, backtest_symbols
from app.backtest.fusion_backtest import (
    FusionBacktestParams,
    fusion_params_from_config,
    run_fusion_backtest,
    run_fusion_matrix,
)
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


# ----------------------------------------------------------
# 融合策略回测（报告 G1 / PRD §8 融合策略两层回测）
# ----------------------------------------------------------
class FusionBacktestRequest(BaseModel):
    symbols: Optional[list[str]] = Field(None, description="缺省=全部主连品种")
    start: Optional[str] = Field(None, description="起始日 YYYY-MM-DD（默认全历史）")
    end: Optional[str] = Field(None, description="结束日 YYYY-MM-DD（默认至今）")
    src: Optional[str] = Field(None, description="单一小时线口径：akshare/tqsdk")
    sl_atr: Optional[float] = None
    trail_atr: Optional[float] = None
    be_r: Optional[float] = None
    W: Optional[int] = None
    entry_mode: Optional[str] = None
    cost_bp: Optional[float] = None
    max_positions: Optional[int] = None
    seed_jitter: Optional[float] = Field(0.0, description="多 seed 鲁棒性：sl/trail 相对扰动幅度")
    seeds: Optional[list[int]] = Field(None, description="非空则对每个 seed 各跑一遍取均值")
    async_run: bool = True


def _parse_date(s: str | None):
    if not s:
        return None
    from datetime import datetime as _dt

    return _dt.strptime(s, "%Y-%m-%d").date()


@router.post("/fusion")
def run_fusion(req: FusionBacktestRequest, bg: BackgroundTasks):
    """融合策略回测：信号层 walk-forward（复用 fusion_state_detail 同一循环）+ 组合层 FIFO 重放。

    返回：组合层指标（笔数/胜率/总收益%/最大回撤%/夏普）+ 逐品种明细 + 净值曲线。
    """
    rd = _readiness()
    if not rd.get("ready"):
        raise HTTPException(503, detail={"message": "数据未就绪，拒绝回测", "readiness": rd})

    def _task():
        try:
            overrides = {k: v for k, v in {
                "src": req.src, "sl_atr": req.sl_atr, "trail_atr": req.trail_atr,
                "be_r": req.be_r, "W": req.W, "entry_mode": req.entry_mode,
                "cost_bp": req.cost_bp, "max_positions": req.max_positions,
                "seed_jitter": req.seed_jitter,
            }.items() if v is not None}
            p = fusion_params_from_config(overrides)
            with session_scope() as s:
                repo = TaskRepository(s)
                run = repo.start(
                    "fusion_backtest", label="manual",
                    payload={k: v for k, v in req.model_dump().items() if v is not None},
                )
                res = run_fusion_backtest(
                    s, symbols=req.symbols, params=p,
                    start=_parse_date(req.start), end=_parse_date(req.end),
                    seeds=req.seeds,
                )
                ok = res.get("n_trades", 0)
                repo.finish(run, "success", f"trades={ok}, win={res.get('win_rate')}")
                logger.info(f"[fusion_backtest] done trades={ok}")
                return res
        except Exception as e:
            logger.exception(f"[fusion_backtest] failed: {e}")
            return {"error": str(e)}

    if req.async_run:
        bg.add_task(_task)
        return {"status": "scheduled", "message": "融合策略回测在后台执行"}
    return _task()


class FusionMatrixRequest(BaseModel):
    symbols: Optional[list[str]] = Field(None, description="缺省抽样前 10 主连控时")
    start: Optional[str] = None
    end: Optional[str] = None
    grid: Optional[dict] = Field(None, description="自定义网格 {src:[],sl_atr:[],trail_atr:[]}")


@router.post("/fusion/matrix")
def run_fusion_matrix_api(req: FusionMatrixRequest, bg: BackgroundTasks):
    """四维测试矩阵（§8.3 简化版）：src × sl_atr × trail_atr 网格扫描，汇总净收益/胜率。"""
    rd = _readiness()
    if not rd.get("ready"):
        raise HTTPException(503, detail={"message": "数据未就绪，拒绝回测", "readiness": rd})

    def _task():
        try:
            with session_scope() as s:
                repo = TaskRepository(s)
                run = repo.start(
                    "fusion_matrix", label="manual",
                    payload={k: v for k, v in req.model_dump().items() if v is not None},
                )
                res = run_fusion_matrix(
                    s, symbols=req.symbols,
                    start=_parse_date(req.start), end=_parse_date(req.end),
                    grid=req.grid,
                )
                repo.finish(run, "success", f"{len(res['rows'])} cells")
                logger.info(f"[fusion_matrix] done cells={len(res['rows'])}")
        except Exception as e:
            logger.exception(f"[fusion_matrix] failed: {e}")

    bg.add_task(_task)
    return {"status": "scheduled", "message": "融合策略矩阵在后台执行"}


@router.post("/update-weights")
def update_weights():
    """⑳ 手动触发权重月更（按最新回测近 60 日准确率）"""
    from app.backtest.weights import update_model_weights

    with session_scope() as s:
        res = update_model_weights(s)
    return res