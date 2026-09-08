from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SymbolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str
    exchange: str
    unit: str | None = None
    multiplier: float | None = None
    product: str | None = None
    is_main: bool
    main_symbol: str | None = None
    active: bool