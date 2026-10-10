"""被剔除（不再观察/交易）的品种封禁清单。

来源：用户 2026-10-10 在 varieties.csv 标注「否」的品种（低持仓/低成交）。
凡是「每日从 akshare 全量重插」的入口（dim_variety 字典重建、spot_basis 采集、
main_contract_map 派生、contract_daily 采集等）都必须经过本清单过滤，否则被删的
品种会在夜间重建时被重新插回（dim_variety/spot_basis 的采集默认 products=None=全品种）。

注意：只放「品种码」，不要放合约码（CS2011 之类）。匹配大小写不敏感。
"""
from __future__ import annotations

# 用户剔除的品种（原生大小写）
_BLOCKED = {
    'CS', 'B', 'RR', 'BZ', 'BB', 'FB', 'LG',           # DCE
    'PL', 'CY', 'RS', 'JR', 'LR', 'ME', 'PM', 'RI', 'TC', 'WH', 'ZC',  # CZCE
    'AD', 'OP', 'WR',                                 # SHFE
    'NR', 'BC',                                       # INE
    'PD', 'PT',                                       # GFEX
    'IC', 'IF', 'IH', 'IM', 'T', 'TF', 'TL', 'TS',    # CFFEX（金融期货）
}

BLOCKED_VARIETY_CODES = frozenset(c.upper() for c in _BLOCKED)


def is_blocked(code: str | None) -> bool:
    """品种码是否被封禁（大小写不敏感）。"""
    if not code:
        return False
    return str(code).upper() in BLOCKED_VARIETY_CODES


import re as _re

_SYMBOL_RE = _re.compile(r"^([A-Za-z]+)(\d+)$")


def is_blocked_symbol(symbol: str | None) -> bool:
    """合约码（如 ``IF2601`` / ``RB888``）是否属被剔品种。

    取合约码的前导字母作为品种码去匹配封禁清单；剩余部分须为数字
    （区分 ``IF2601``→IF 被剔，``I2601``→I 保留；``TA2601``→TA 保留，``T2601``→T 被剔）。
    """
    if not symbol:
        return False
    m = _SYMBOL_RE.match(str(symbol).strip())
    if not m:
        return False
    return m.group(1).upper() in BLOCKED_VARIETY_CODES


def blocked_list() -> list[str]:
    return sorted(BLOCKED_VARIETY_CODES)
