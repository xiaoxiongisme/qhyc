"""
FastAPI 入口（M1–M5）
- ROLE=api 时启动 Web 服务（含 M5 React 看板静态托管）
- ROLE=scheduler 时仅调度（参见 app.scheduler）
- ROLE=all 时两者一起跑（开发态）
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.core.config import get_settings
from app.core.db import get_engine, session_scope
from app.core.logging import logger, setup_logging

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info(
        f"[main] startup version={__version__} role={settings.env.ROLE} "
        f"db_host={settings.env.POSTGRES_HOST}"
    )
    # 启动时校验 DB 连通
    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        logger.info("[main] DB connection ok")
        # R3②：品种清单以 config 为唯一事实源，启动时幂等同步
        from app.core.bootstrap import sync_symbols

        with session_scope() as s:
            sync_symbols(s)
    except Exception as e:
        logger.error(f"[main] DB connection failed: {e}")
    yield
    logger.info("[main] shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="期货预测平台 API",
        version=__version__,
        description="国内期货涨跌预测平台（M1 数据层 + M2/M3 预测引擎 + M4 回测 + M5 看板）",
        lifespan=lifespan,
    )
    # API 路由优先注册
    app.include_router(api_router)

    @app.get("/api-info")
    def api_info():
        return {
            "service": "qhyc",
            "version": __version__,
            "milestone": "M5",
            "caliber": get_settings().yaml.backtest.label_metric,
            "docs": "/docs",
            "dashboard": "/",
        }

    # M5：React 看板静态托管（注册在 API 路由之后，兜底 catch-all）
    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIST / "index.html")

        logger.info("[main] dashboard static mounted from web/dist")
    else:

        @app.get("/", include_in_schema=False)
        def root():
            return {
                "service": "qhyc",
                "version": __version__,
                "milestone": "M5",
                "docs": "/docs",
                "note": "看板未构建（web/dist 不存在）",
            }

    return app


app = create_app()


def main():
    """uvicorn 入口（容器内调用）"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.env.API_HOST,
        port=settings.env.API_PORT,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()