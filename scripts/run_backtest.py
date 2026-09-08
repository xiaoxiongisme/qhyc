"""
CLI：M4 回测（PRD §6）
- 用法：docker compose exec api python scripts/run_backtest.py [--symbols FG888,SA888]
       [--test-days 60] [--step 3] [--update-weights]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db import session_scope
from app.core.logging import logger, setup_logging


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default=None)
    ap.add_argument("--test-days", type=int, default=60)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--update-weights", action="store_true", help="回测后立即更新模型权重")
    args = ap.parse_args()

    from app.backtest.engine import BacktestParams, backtest_symbols
    from app.core.config import get_settings

    symbols = (
        [x.strip().upper() for x in args.symbols.split(",")]
        if args.symbols
        else None
    )

    with session_scope() as s:
        params = BacktestParams(test_days=args.test_days, step=args.step)
        out = backtest_symbols(s, symbols=symbols, params=params)
        for r in out:
            if "metrics" in r:
                best = sorted(
                    ((k, v) for k, v in r["metrics"].items() if v),
                    key=lambda kv: -(kv[1].get("dir_acc") or 0),
                )
                line = " ".join(f"{k}={v['dir_acc']}" for k, v in best[:5])
                logger.info(f"[bt] {r['symbol']}: {line}")

        if args.update_weights:
            from app.backtest.weights import update_model_weights

            res = update_model_weights(s)
            logger.info(f"权重更新: {res}")


if __name__ == "__main__":
    main()