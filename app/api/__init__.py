"""FastAPI 路由聚合（M1 + M2 预测）"""
from fastapi import APIRouter

from app.api.routers import (
    anomalies,
    backtest,
    bars,
    calendar,
    dashboard,
    fusion,      # 融合策略推送留痕（防漏看回查）
    health,
    imports,
    ingest,
    pipeline,    # M8 决策链路容器化
    position,    # §18.4/§18.5 M6a
    predict,
    symbols,
    tasks,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="", tags=["health"])
api_router.include_router(symbols.router, prefix="/symbols", tags=["symbols"])
api_router.include_router(bars.router, prefix="/bars", tags=["bars"])
api_router.include_router(ingest.router, prefix="/ingest", tags=["ingest"])
api_router.include_router(anomalies.router, prefix="/anomalies", tags=["anomalies"])
api_router.include_router(calendar.router, prefix="/calendar", tags=["calendar"])
api_router.include_router(imports.router, prefix="/imports", tags=["imports"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(predict.router, prefix="/predict", tags=["predict"])
api_router.include_router(backtest.router, prefix="/backtest", tags=["backtest"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(position.router, prefix="", tags=["position"])  # §18.4/§18.5 M6a
api_router.include_router(fusion.router, prefix="", tags=["fusion"])
# M8 决策链路容器化（docs/M8_决策链路容器化_PRD_20260922.md §8）
api_router.include_router(pipeline.router, prefix="/pipeline", tags=["pipeline"])

__all__ = ["api_router"]