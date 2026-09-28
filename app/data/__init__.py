# -*- coding: utf-8 -*-
"""数据层 L2（六层解耦）：口径注册 + 统一取数入口。

对外只暴露 BarStore 与口径常量；上层（采集/回测/因子/策略/应用）
**不得**绕过本层直接裸写 SQL 查行情表。
"""

from app.data.barstore import (
    SymbolNotFoundError,
    coverage,
    load,
    load_many,
    namespace_conflicts,
    resolve_symbol,
)
from app.data.caliber import CALIBERS, DEFAULT_CALIBER, FREQS, CaliberError, get_route

__all__ = [
    "load", "load_many", "resolve_symbol", "namespace_conflicts", "coverage",
    "SymbolNotFoundError",
    "CALIBERS", "DEFAULT_CALIBER", "FREQS", "CaliberError", "get_route",
]
