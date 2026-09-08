"""§16.5 数据层新增 ORM：sector_map / sector_index / transmission_weights"""
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SectorMap(Base):
    __tablename__ = "sector_map"

    product: Mapped[str] = mapped_column(Text, primary_key=True)
    sector: Mapped[str] = mapped_column(Text, nullable=False)
    chain_role: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    src: Mapped[str] = mapped_column(Text, nullable=False, default="yaml")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SectorIndex(Base):
    __tablename__ = "sector_index"

    sector: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ret_1d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ret_5d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ret_20d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    index_level: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    weight_method: Mapped[str] = mapped_column(String(16), nullable=False, default="volume")
    composition: Mapped[dict | None] = mapped_column(JSON)


class TransmissionWeight(Base):
    __tablename__ = "transmission_weights"

    src_product: Mapped[str] = mapped_column(Text, primary_key=True)
    dst_product: Mapped[str] = mapped_column(Text, primary_key=True)
    method: Mapped[str] = mapped_column(Text, primary_key=True)
    lag_days: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False, default="positive")
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    cost_ratio: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelWeight(Base):
    """⑳ 模型权重（按近 60 日回测准确率月更）"""
    __tablename__ = "model_weights"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    dir_acc: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    sample_n: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="backtest")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )