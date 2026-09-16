"""M6c.1 训练池化分类模型并落盘（与 LSTM 周更同机制）

用法（容器内）：
  python scripts/train_pooled.py [--train-end YYYY-MM-DD]
  --train-end 给定时仅用其之前数据训练（生成防泄漏快照，供回测按 eval_date 调用）

只读 daily_bar，训练后写入 /app/runtime/pooled/{rf,xgb}_pool.joblib
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

from app.core.db import session_scope
from app.predictors.pooled import PooledRFModel, PooledXGBModel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-end", default=None, help="防泄漏快照截止日 YYYY-MM-DD（默认全历史）")
    args = ap.parse_args()

    with session_scope() as s:
        for cls in (PooledRFModel, PooledXGBModel):
            m = cls()
            meta = m.fit_from_db(s, train_end=args.train_end)
            print(f"[{cls.name}] 训练完成：n_train={meta['n_train']} "
                  f"train_end={meta['train_end']} -> {meta['feature_names']}")


if __name__ == "__main__":
    main()
