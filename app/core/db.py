"""
数据库连接（同步 SQLAlchemy 2.x + psycopg3）
- M1 主要使用同步引擎：采集 / 校准 / 导入 都是 IO/计算密集型，sqlalchemy 同步更直观
- 预留 AsyncSession（FastAPI 路由使用 sync session 即可，简化依赖）
"""
from __future__ import annotations

import threading
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

#: 保护 ``_engine`` / ``_SessionLocal`` 这一对**必须同步替换**的全局量。
#: 见 :func:`rebind_engine` 的 P0-2 说明：只换其一即为静默失效。
_rebind_lock = threading.RLock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _rebind_lock:
            if _engine is None:  # 双检：避免并发重复建引擎
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
        with _rebind_lock:
            if _SessionLocal is None:  # 双检
                _SessionLocal = _make_session_factory(get_engine())
    return _SessionLocal


def _make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def rebind_engine(
    url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    dispose_old: bool = True,
    clear_fee_cache: bool = True,
) -> Engine:
    """官方外部引擎切换入口（替代对模块级 ``get_engine`` 的 monkey-patch）。

    背景（2026-10-08 工单）：回测脚本要在容器内把引擎指向云端 SSH 隧道
    （``host.docker.internal:15432``），此前**没有官方开关**，只能 setattr 覆盖
    ``app.data.cost.get_engine`` / ``app.data.barstore.get_engine`` 这类**模块级
    私有名字**。CB 一旦重命名或改导入方式，外部脚本就静默失效——本项目最典型的
    静默失效陷阱。

    ⚠ P0-2 陷阱（本函数存在的首要理由）
    ----------------------------------
    ``get_session_factory()`` 里的 ``sessionmaker(bind=get_engine())`` 持有的是
    **引擎的当次快照**。只替换 ``_engine`` 而不替换 ``_SessionLocal``，则所有走
    ``session_scope()`` 的代码（含 ``fee_per_lot``）**仍然用旧引擎**，重绑定
    「看似生效实则无效」——这是最坏的一类 bug：无报错、数字照旧、只是没换库。
    因此本函数在**同一把锁内原子完成两处替换**，并把 ``_SessionLocal`` 重新绑定
    到新引擎（而不是置 None 走惰性重建，避免中间态窗口）。

    参数
    ----
    url:
        目标库 URL；``None`` 表示按当前 settings 重建。
    pool_size / max_overflow:
        ``None`` 表示沿用 settings（``config/*.yaml: database``，并支持
        ``QH_PG_POOL_SIZE`` / ``QH_PG_MAX_OVERFLOW`` 环境变量覆盖）。
    dispose_old:
        先 ``engine.dispose()`` 归还旧池连接。**隧道场景必须为 True**，
        否则旧池的连接句柄会一直挂着，直到进程退出。
    clear_fee_cache:
        换库 = 换数据。费率缓存的键**不含引擎/库标识**，跨库复用会把 A 库的费率
        喂给 B 库。默认清空；若调用方明确知道两库费率一致可传 False 提速。

    返回
    ----
    新建的 :class:`~sqlalchemy.engine.Engine` 实例。
    """
    global _engine, _SessionLocal

    with _rebind_lock:
        settings = get_settings()
        target_url = url or settings.db_url
        eff_pool = pool_size if pool_size is not None else settings.yaml.database.pool_size
        eff_over = max_overflow if max_overflow is not None else settings.yaml.database.max_overflow
        new_engine = create_engine(
            target_url,
            pool_size=eff_pool,
            max_overflow=eff_over,
            echo=settings.yaml.database.echo,
            pool_pre_ping=True,
            future=True,
        )

        old_engine = _engine
        # ---- 原子替换：两个全局量必须一起换（P0-2）----
        _engine = new_engine
        _SessionLocal = _make_session_factory(new_engine)

        if old_engine is not None and dispose_old:
            try:
                old_engine.dispose()
            except Exception as exc:  # noqa: BLE001
                # 归还旧池失败不应阻断重绑定，但要留痕（否则句柄悬挂无从排查）
                print(f"[db] rebind_engine: dispose 旧引擎失败（已忽略）: {exc}")

    # 锁外清缓存：cost 依赖 db，放锁内会引入反向依赖死锁风险
    if clear_fee_cache:
        try:
            from app.data.cost import clear_fee_cache as _clear

            _clear()
        except Exception as exc:  # noqa: BLE001
            print(f"[db] rebind_engine: 清费率缓存失败（已忽略）: {exc}")

    try:
        from app.core.logging import logger

        logger.info(
            f"[db] rebind_engine -> {target_url.rsplit('@', 1)[-1]} "
            f"(pool_size={eff_pool}, max_overflow={eff_over}, "
            f"dispose_old={dispose_old}, fee_cache_cleared={clear_fee_cache})"
        )
    except Exception:  # noqa: BLE001
        pass
    return new_engine


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


__all__ = ["get_engine", "get_session_factory", "session_scope", "fastapi_db_dep",
           "rebind_engine"]