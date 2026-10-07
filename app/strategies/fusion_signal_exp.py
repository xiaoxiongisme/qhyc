"""``fusion_signal_exp`` - stable alias module for app.strategies.fusion_signal.

Why (2026-10-07)
-----------------
External backtest scripts (WB side, e.g. run_param_scan_v3.py) historically
imported the fusion engine by the name ``fusion_signal_exp``::

    from fusion_signal_exp import walk_fusion_states, ema, atr14, adx14

But the repo only has the canonical implementation
``app/strategies/fusion_signal.py`` - there was NO such alias module
(``find / -name "fusion_signal_exp*"`` returned nothing in the container).
Consequence: WB had to ``docker cp`` a temporary bridge file into /tmp on every
run, and any image rebuild wiped it -> ``ModuleNotFoundError: No module named
'fusion_signal_exp'`` (actually happened 2026-10-07 11:15).

This module fixes the alias inside the repo/image so the docker-cp bridge is no
longer required (work order A1, option 1 - simplest and root-cause).

Relationship to the canonical implementation (SAME code, not a copy)
--------------------------------------------------------------------
- walk_fusion_states / fusion_state_detail / fusion_state: the single source of
  truth for the state machine and the pyramiding (add-on) logic.
- ema / atr14 / adx14: indicator implementations (ewm adjust=False / Wilder RMA).

Discipline: this module ONLY re-exports; it duplicates no algorithm code. Any
engine change is made in fusion_signal.py alone and the alias follows
automatically - this deliberately avoids the "two copies drift apart" trap that
the repo has hit before (multiple SPEC / caliber variants coexisted).
"""
from __future__ import annotations

from app.strategies.fusion_signal import (  # noqa: F401
    adx14,
    atr14,
    ema,
    fusion_state,
    fusion_state_detail,
    walk_fusion_states,
)

__all__ = [
    "walk_fusion_states",
    "fusion_state_detail",
    "fusion_state",
    "ema",
    "atr14",
    "adx14",
]
