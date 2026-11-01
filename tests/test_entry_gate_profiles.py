"""final_v2/final_v3 入场门控 profile 契约。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.strategies import entry_gates as eg


def _regime_up():
    return pd.Series([1.0], index=pd.to_datetime(["2026-01-01"]))


def _hourly():
    return pd.to_datetime(["2026-01-02 10:00:00"]).to_numpy()


def test_regime_short_switch_is_isolated_from_k45():
    both = eg.build_entry_gates(_hourly(), None, _regime_up())
    exempt = eg.build_entry_gates(
        _hourly(), None, _regime_up(), regime_short_enabled=False)
    assert (int(both[0][0]), int(both[1][0])) == (1, 0)
    assert (int(exempt[0][0]), int(exempt[1][0])) == (1, 1)


def test_final_v3_whitelist_and_k45_not_exempt(monkeypatch):
    monkeypatch.setattr(eg, "get_market_regime", lambda *_args: _regime_up())
    monkeypatch.setattr(eg, "get_k45_state", lambda *_args: None)
    df = pd.DataFrame({"dt": _hourly()})
    lh = eg.gates_for_symbol(None, "LH888", df, [], profile="final_v3")
    rb = eg.gates_for_symbol(None, "RB888", df, [], profile="final_v3")
    assert (int(lh[0][0]), int(lh[1][0])) == (1, 1)
    assert (int(rb[0][0]), int(rb[1][0])) == (1, 0)

    mid = pd.DataFrame({"prog": [0.40]}, index=pd.to_datetime(["2026-01-01"]))
    monkeypatch.setattr(eg, "get_k45_state", lambda *_args: mid)
    lh_mid = eg.gates_for_symbol(None, "LH888", df, [], profile="final_v3")
    assert (int(lh_mid[0][0]), int(lh_mid[1][0])) == (0, 0)


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="entry_gate_profile"):
        eg.gates_for_symbol(None, "FG888", pd.DataFrame({"dt": _hourly()}), [],
                            profile="not_a_profile")
