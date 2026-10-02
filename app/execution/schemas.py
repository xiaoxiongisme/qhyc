# -*- coding: utf-8 -*-
"""P0-1 执行反解层 — 接口契约（pydantic）。

信号（连续 888 / 原始连续价空间） → 可执行真实合约订单建议。
字段定义见 docs/PRD_P0-1_执行反解层_20261002.md §2。

实测勘误（2026-10-02 探针）：hourly_bar.<SYM888>.close 为**原始连续价**，
故 price_space 默认 "raw"（偏移置 0）。若上游信号确为已后复权价，传 "adj"。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class ReverseRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str                                    # 连续主力码，如 RB888
    trade_date: date                               # 信号交易日（换月映射 + 偏移 as-of）
    trade_datetime: Optional[datetime] = None      # 精确到 K 的时点；缺省用 trade_date 收盘
    direction: str = Field(..., pattern="^(LONG|SHORT|FLAT)$")
    entry_px: Optional[float] = None               # 信号入场价（连续/原始连续价空间）
    stop_px: Optional[float] = None                # 信号止损价（同空间）
    lots: Optional[int] = None                     # 信号给定手数（与 target_risk 二选一）
    target_risk: Optional[float] = None            # 单笔目标风险（元）；优先于 lots
    atr: Optional[float] = None                    # 入场 ATR（按风险算手数时需要）
    # 价空间：raw=原始连续价（888 序列实测如此，默认）；adj=已后复权价（real=adj-offset）
    price_space: Optional[str] = Field(None, pattern="^(raw|adj)$")


class ExecutionOrder(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    real_symbol: Optional[str] = None              # 反解出的真实可交易合约，如 FG2701（main_contract_map.underlying）
    exchange: Optional[str] = None
    product: Optional[str] = None
    direction: str
    price: Optional[float] = None                  # 原始（未复权）价，下单用
    stop_price: Optional[float] = None
    lots: int = 0                                  # 整数手；阻断时置 0
    multiplier: Optional[float] = None
    notional: Optional[float] = None
    tick: Optional[float] = None
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocking_reasons
