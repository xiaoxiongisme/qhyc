"""只读探针：查清 backtest_detail 结构，判断能否做离线阈值扫描。

关键问题：
  1. backtest_detail 是否保存"全部评估点"（含 signaled=False 行）？
     - 若是 -> 可用同一批 (symbol, eval_date) 锚点，离线扫多个 gate，无需多次全量重跑。
     - 若否（只存 signaled）-> 需另取 eval_date 全集（改用回测引擎重建）。
  2. 是否已存 ret_t / gate_reason 等字段（可直接用于门控重算）？
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope

RUN = "20260910_143938_bt"

with session_scope() as s:
    cols = pd.read_sql(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='backtest_detail' ORDER BY ordinal_position"
        ),
        s.get_bind(),
    )
    print("=== backtest_detail columns ===")
    print(cols.to_string(index=False))

    per = pd.read_sql(
        text(
            "SELECT model, COUNT(*) AS n, "
            "SUM(CASE WHEN signaled THEN 1 ELSE 0 END) AS sig "
            "FROM backtest_detail WHERE run_id=:r GROUP BY model ORDER BY model"
        ),
        s.get_bind(),
        params={"r": RUN},
    )
    print("\n=== per-model detail rows (run=%s) ===" % RUN)
    print(per.to_string(index=False))

    # reversal 抽样 8 行，看有没有 ret 相关列
    smp = pd.read_sql(
        text(
            "SELECT * FROM backtest_detail WHERE run_id=:r AND model='reversal' "
            "ORDER BY symbol, eval_date LIMIT 8"
        ),
        s.get_bind(),
        params={"r": RUN},
    )
    print("\n=== reversal sample rows ===")
    print(smp.to_string(index=False))
