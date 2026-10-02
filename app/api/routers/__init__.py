"""M1 路由子包"""
from app.api.routers import (
    anomalies,
    bars,
    calendar,
    execution,
    health,
    imports,
    ingest,
    symbols,
    tasks,
)

__all__ = [
    "anomalies",
    "bars",
    "calendar",
    "execution",
    "health",
    "imports",
    "ingest",
    "symbols",
    "tasks",
]