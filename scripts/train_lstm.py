"""
CLI：LSTM 周度重训（⑱）
- 对全部主连品种（有足够数据的）训练并保存权重到 /app/runtime/lstm/
- 由 APScheduler 每周六 06:00 触发；也可手动执行
- 用法：docker compose exec api python scripts/train_lstm.py [--symbols FG888,SA888]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.features.pipeline import build_features
from app.predictors.lstm_train import _ensure_torch, train_symbol


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default=None, help="逗号分隔；缺省=全部主连品种")
    ap.add_argument("--epochs", type=int, default=30)
    args = ap.parse_args()

    torch = _ensure_torch()
    from app.core.config import get_settings

    settings = get_settings()
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")]
        if args.symbols
        else [s.symbol for s in settings.main_contracts]
    )

    results = []
    with session_scope() as s:
        for sym in symbols:
            try:
                snap = build_features(s, sym)
                # §16.4 多变量：附加传导特征 v1（滞后口径）
                from app.features.transmission import build_transmission_frame

                tframe = build_transmission_frame(s, sym, end=snap.last_date)
                r = train_symbol(
                    torch, sym, snap.rets,
                    dates=snap.dates, extra=tframe,
                    seq_len=settings.yaml.predict.lstm.seq_len,
                    epochs=args.epochs,
                )
            except Exception as e:
                r = {"symbol": sym, "error": str(e)}
            results.append(r)
            logger.info(f"[lstm-train] {r}")

    ok = sum(1 for r in results if "error" not in r and "skipped" not in r)
    logger.info(f"[lstm-train] 完成 {ok}/{len(results)}")


if __name__ == "__main__":
    main()
