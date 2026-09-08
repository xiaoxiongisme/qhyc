"""
FastAPI 入口（M1）
- ROLE=api 时启动 Web 服务
- ROLE=scheduler 时仅调度（参见 app.scheduler）
- ROLE=all 时两者一起跑（开发态）
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import api_router
from app.core.config import get_settings
from app.core.db import get_engine, session_scope
from app.core.logging import logger, setup_logging


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
        description="国内期货涨跌预测平台（M1 数据层）",
        lifespan=lifespan,
    )
    app.include_router(api_router)

    @app.get("/")
    def root():
        return {
            "service": "qhyc",
            "version": __version__,
            "milestone": "M1",
            "docs": "/docs",
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