"""SQLAlchemy ORM 模型（M1）

表结构与 db/init/01_schema.sql 严格对齐，类型使用 SQLAlchemy 标准类型；
numeric(20,4) → Numeric(20,4)；numeric(12,6) → Numeric(12,6)。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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


# 通用类型别名
Numeric20 = Numeric(20, 4)
Numeric12 = Numeric(12, 6)
Numeric6 = Numeric(6, 4)


# -----------------------------------------------------
# 元数据
# -----------------------------------------------------
class FuturesSymbol(Base):
    __tablename__ = "futures_symbol"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)
    multiplier: Mapped[Decimal | None] = mapped_column(Numeric20)
    product: Mapped[str | None] = mapped_column(Text)
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    main_symbol: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# -----------------------------------------------------
# 主连映射 / 换月
# -----------------------------------------------------
class MainContractMap(Base):
    __tablename__ = "main_contract_map"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    exchange: Mapped[str] = mapped_column(Text, primary_key=True)
    product: Mapped[str] = mapped_column(Text, primary_key=True)
    main_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    underlying: Mapped[str] = mapped_column(Text, nullable=False)
    change_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delta: Mapped[Decimal] = mapped_column(Numeric20, nullable=False, default=Decimal("0"))
    src: Mapped[str] = mapped_column(Text, nullable=False, default="csv")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 日线（超表）
# -----------------------------------------------------
class DailyBar(Base):
    __tablename__ = "daily_bar"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric20)
    high: Mapped[Decimal | None] = mapped_column(Numeric20)
    low: Mapped[Decimal | None] = mapped_column(Numeric20)
    close: Mapped[Decimal | None] = mapped_column(Numeric20)
    settle: Mapped[Decimal | None] = mapped_column(Numeric20)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    oi: Mapped[int | None] = mapped_column(BigInteger)
    ret_close: Mapped[Decimal | None] = mapped_column(Numeric12)
    ret_settle: Mapped[Decimal | None] = mapped_column(Numeric12)
    ret5: Mapped[Decimal | None] = mapped_column(Numeric12)
    ret20: Mapped[Decimal | None] = mapped_column(Numeric12)
    src: Mapped[str] = mapped_column(Text, nullable=False, default="akshare")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# -----------------------------------------------------
# 主连平滑/原始（超表）
# -----------------------------------------------------
class MainContinuous(Base):
    __tablename__ = "main_continuous"

    product: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    raw_open: Mapped[Decimal | None] = mapped_column(Numeric20)
    raw_high: Mapped[Decimal | None] = mapped_column(Numeric20)
    raw_low: Mapped[Decimal | None] = mapped_column(Numeric20)
    raw_close: Mapped[Decimal | None] = mapped_column(Numeric20)
    raw_volume: Mapped[int | None] = mapped_column(BigInteger)
    raw_oi: Mapped[int | None] = mapped_column(BigInteger)
    adj_open: Mapped[Decimal | None] = mapped_column(Numeric20)
    adj_high: Mapped[Decimal | None] = mapped_column(Numeric20)
    adj_low: Mapped[Decimal | None] = mapped_column(Numeric20)
    adj_close: Mapped[Decimal | None] = mapped_column(Numeric20)
    adj_volume: Mapped[int | None] = mapped_column(BigInteger)
    adj_oi: Mapped[int | None] = mapped_column(BigInteger)
    underlying: Mapped[str | None] = mapped_column(Text)
    change_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    src: Mapped[str] = mapped_column(Text, nullable=False, default="csv")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 小时线（超表，M6 启用）
# -----------------------------------------------------
class HourlyBar(Base):
    __tablename__ = "hourly_bar"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_datetime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric20)
    high: Mapped[Decimal | None] = mapped_column(Numeric20)
    low: Mapped[Decimal | None] = mapped_column(Numeric20)
    close: Mapped[Decimal | None] = mapped_column(Numeric20)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)
    ret: Mapped[Decimal | None] = mapped_column(Numeric12)
    src: Mapped[str] = mapped_column(Text, nullable=False, default="akshare")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 交易日历
# -----------------------------------------------------
class TradeCalendar(Base):
    __tablename__ = "trade_calendar"

    exchange: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sessions: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 预测结果
# -----------------------------------------------------
class PredictionResult(Base):
    __tablename__ = "prediction_result"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    as_of_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_set: Mapped[str] = mapped_column(Text, nullable=False, default="m2")
    direction: Mapped[str | None] = mapped_column(Text)
    direction_prob: Mapped[Decimal | None] = mapped_column(Numeric6)
    ret_point: Mapped[Decimal | None] = mapped_column(Numeric12)
    ret_low: Mapped[Decimal | None] = mapped_column(Numeric12)
    ret_high: Mapped[Decimal | None] = mapped_column(Numeric12)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric6)
    state: Mapped[str | None] = mapped_column(Text)
    participated_models: Mapped[list | None] = mapped_column(JSON)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 回测（P0 修复：主键含 symbol，多品种不再互相覆盖）
# -----------------------------------------------------
class BacktestResult(Base):
    __tablename__ = "backtest_result"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    # 列名 window_len（window 为 PG 保留字）
    window_len: Mapped[int] = mapped_column(Integer, nullable=False, default=250)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    dir_acc: Mapped[Decimal | None] = mapped_column(Numeric6)
    mae: Mapped[Decimal | None] = mapped_column(Numeric12)
    rmse: Mapped[Decimal | None] = mapped_column(Numeric12)
    quantile_hit: Mapped[Decimal | None] = mapped_column(Numeric6)
    sample_n: Mapped[int | None] = mapped_column(Integer)
    by_state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 回测逐点明细（审计 P2-7：支撑看板下钻与误判归因）
# -----------------------------------------------------
class BacktestDetail(Base):
    __tablename__ = "backtest_detail"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    eval_date: Mapped[date] = mapped_column(Date, primary_key=True)
    state: Mapped[str | None] = mapped_column(Text)
    pred_dir: Mapped[str | None] = mapped_column(Text)
    prob: Mapped[Decimal | None] = mapped_column(Numeric6)
    point: Mapped[Decimal | None] = mapped_column(Numeric12)
    low: Mapped[Decimal | None] = mapped_column(Numeric12)
    high: Mapped[Decimal | None] = mapped_column(Numeric12)
    actual: Mapped[Decimal | None] = mapped_column(Numeric12)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 异常工单
# -----------------------------------------------------
class AnomalyTicket(Base):
    __tablename__ = "anomaly_ticket"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    akshare_val: Mapped[Decimal | None] = mapped_column(Numeric20)
    tqsdk_val: Mapped[Decimal | None] = mapped_column(Numeric20)
    diff: Mapped[Decimal | None] = mapped_column(Numeric12)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric12)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# -----------------------------------------------------
# 简报引擎信号
# -----------------------------------------------------
class BriefingSignal(Base):
    __tablename__ = "briefing_signal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    direction: Mapped[str | None] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric6)
    note: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# -----------------------------------------------------
# 任务流水
# -----------------------------------------------------
class TaskRun(Base):
    __tablename__ = "task_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    payload: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(Text)