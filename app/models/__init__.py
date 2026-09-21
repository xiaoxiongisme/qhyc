"""SQLAlchemy ORM 模型（M1/M2/M3/M6a）"""
from app.models.base import Base
from app.models.domain import (
    FuturesSymbol,
    MainContractMap,
    DailyBar,
    MainContinuous,
    HourlyBar,
    TradeCalendar,
    PredictionResult,
    BacktestResult,
    AnomalyTicket,
    BriefingSignal,
    TaskRun,
    MemberPositionRank,   # §18.4 M6a
    Inventory,            # §18.5 M6a
    SpotBasis,            # §18.5（v1.3.2）基差因子
    ContractDaily,        # §18.6 M6b 合约级日线
    FutKline,              # futures-data-fetch 技能适配层
    ContractCodeMap,       # 合约代码对照表（全库统一 4 位标准码）
)
from app.models.sector import SectorMap, SectorIndex, TransmissionWeight, ModelWeight
from app.models.domain import BacktestDetail

__all__ = [
    "Base",
    "FuturesSymbol",
    "MainContractMap",
    "DailyBar",
    "MainContinuous",
    "HourlyBar",
    "TradeCalendar",
    "PredictionResult",
    "BacktestResult",
    "AnomalyTicket",
    "BriefingSignal",
    "TaskRun",
    "SectorMap",
    "SectorIndex",
    "TransmissionWeight",
    "ModelWeight",
    "MemberPositionRank",
    "Inventory",
    "SpotBasis",
    "ContractDaily",
    "FutKline",
    "ContractCodeMap",
]