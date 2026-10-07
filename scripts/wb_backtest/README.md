# WB backtest scripts (archived into repo 2026-10-07)

Origin: `E:/docker-analysis/2026-10-05-15-35-33/` (WB side, NOT a git repo).
Reason: those scripts were injected via `docker cp` into `qhyc-pipeline:/tmp/`
on every run. Any image rebuild wiped them -> import breakage
(`ModuleNotFoundError: fusion_signal_exp`, actually hit 2026-10-07 11:15).

## Files
- `run_param_scan_v3.py` - fusion param-scan engine (has `_isolate_cost_connections()`,
  now redundant since CB fixed the real leak in app/data/cost.py; kept as-is).
- `run_four.py`       - four-strategy backtest (carries ema/atr NaN carry-last-valid
  + ffb fill fix that un-mangled 15 symbols).
- `run_meanrev.py`    - mean-reversion 15m baseline (builds 15m cache).
- `run_meanrev2.py`   - mean-reversion new direction (entry_k>=3.0 + Wilder ADX gate).

## How to run (inside the container)
    docker cp scripts/wb_backtest/run_param_scan_v3.py qhyc-pipeline:/tmp/
    docker exec -d qhyc-pipeline python /tmp/run_param_scan_v3.py
The scripts still read/write under `/app` (sys.path) and `/tmp` (OUT/CACHE) by
convention - keep that when invoking.

## A1 note (import fix)
`run_param_scan_v3.py` used to do `from fusion_signal_exp import ...`, a flat
module name that only existed as a docker-cp'd bridge in /tmp. It is now
`from app.strategies.fusion_signal import ...` (canonical, same functions).
The alias module `app/strategies/fusion_signal_exp.py` is ALSO kept in the repo
so any other caller using the old flat name keeps working (option 1 of work
order A1) - it only re-exports; no algorithm code is duplicated.
