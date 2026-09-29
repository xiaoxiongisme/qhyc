"""FastAPI 路由聚合（M1 + M2 预测）"""
from fastapi import APIRouter, Depends

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
from app.core.security import verify_api_key

api_router = APIRouter()

# 鉴权依赖（评估文档 §8.1）：除 health 外所有路由统一校验。
# 仅当部署侧在 .env 配置 INTEGRATION_API_KEY 后才会真正拦截，否则放行（见 security.verify_api_key）。
_auth = [Depends(verify_api_key)]

api_router.include_router(health.router, prefix="", tags=["health"])  # 健康检查保持开放
api_router.include_router(symbols.router, prefix="/symbols", tags=["symbols"], dependencies=_auth)
api_router.include_router(bars.router, prefix="/bars", tags=["bars"], dependencies=_auth)
api_router.include_router(ingest.router, prefix="/ingest", tags=["ingest"], dependencies=_auth)
api_router.include_router(anomalies.router, prefix="/anomalies", tags=["anomalies"], dependencies=_auth)
api_router.include_router(calendar.router, prefix="/calendar", tags=["calendar"], dependencies=_auth)
api_router.include_router(imports.router, prefix="/imports", tags=["imports"], dependencies=_auth)
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"], dependencies=_auth)
api_router.include_router(predict.router, prefix="/predict", tags=["predict"], dependencies=_auth)
api_router.include_router(backtest.router, prefix="/backtest", tags=["backtest"], dependencies=_auth)
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"], dependencies=_auth)
api_router.include_router(position.router, prefix="", tags=["position"], dependencies=_auth)  # §18.4/§18.5 M6a
api_router.include_router(fusion.router, prefix="", tags=["fusion"], dependencies=_auth)
# M8 决策链路容器化（docs/M8_决策链路容器化_PRD_20260922.md §8）
api_router.include_router(pipeline.router, prefix="/pipeline", tags=["pipeline"], dependencies=_auth)

__all__ = ["api_router"]