"""ORM 基类"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（与 TimescaleDB 表一一对应）"""