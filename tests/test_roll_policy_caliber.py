# -*- coding: utf-8 -*-
"""丙/丁方案反解层单测（PRD 增补 §6 单测）。

依赖：本地/云端库已跑 seed_follow_rule.py 且 dim_main_contract_inferred 有数据。
核心断言：反解层优先落到 inferred_symbol（888 当日实际所跟合约），而非 main_contract_map.underlying。
"""
from datetime import date

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.execution import roll_policy as rp


def _sample(product: str) -> date | None:
    with session_scope() as s:
        return s.execute(text(
            "SELECT trade_date FROM dim_main_contract_inferred "
            "WHERE variety_code=:p AND inferred_symbol IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 1"), {"p": product}).scalar()


@pytest.mark.parametrize("prod,sym", [("MA", "MA888"), ("EG", "EG888"), ("RB", "RB888")])
def test_resolves_to_inferred(prod, sym):
    d = _sample(prod)
    assert d is not None, f"{prod} 反推数据缺失：先跑 scripts/seed_follow_rule.py"
    with session_scope() as s:
        real, ex, chg, ms = rp.select_real_contract(s, sym, d)
    assert real is not None, f"{sym}@{d} 反解为 None"
    assert real.upper().startswith(prod), f"{sym}@{d} 反解 {real} 不以 {prod} 开头"
    # 丙/丁：反解应等于该日 inferred_symbol
    with session_scope() as s:
        inf = s.execute(text(
            "SELECT inferred_symbol FROM dim_main_contract_inferred "
            "WHERE variety_code=:p AND trade_date=:d"), {"p": prod, "d": d}).scalar()
    assert real.upper() == str(inf).upper(), (
        f"{sym}@{d} 反解 {real} ≠ inferred {inf}（丙/丁要求反解=inferred_symbol）")


def test_non_continuous_returns_none():
    with session_scope() as s:
        assert rp.select_real_contract(s, "MA2609", date(2026, 9, 1)) == (None, None, False, None)
