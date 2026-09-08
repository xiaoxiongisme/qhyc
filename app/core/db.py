"""
数据库连接（同步 SQLAlchemy 2.x + psycopg3）
- M1 主要使用同步引擎：采集 / 校准 / 导入 都是 IO/计算密集型，sqlalchemy 同步更直观
- 预留 AsyncSession（FastAPI 路由使用 sync session 即可，简化依赖）
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import setup_logging

setup_logging()

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.db_url
        _engine = create_engine(
            url,
            pool_size=settings.yaml.database.pool_size,
            max_overflow=settings.yaml.database.max_overflow,
            echo=settings.yaml.database.echo,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """事务级 session scope（自动 commit/rollback）"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def fastapi_db_dep() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个 session"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


__all__ = ["get_engine", "get_session_factory", "session_scope", "fastapi_db_dep"]