"""只读：查看 reversal 的 gate_reason 在 signaled=True / False 两类下的实际文本。"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope

RUN = "20260910_143938_bt"

with session_scope() as s:
    df = pd.read_sql(
        text(
            "SELECT signaled, gate_reason, COUNT(*) AS n "
            "FROM backtest_detail WHERE run_id=:r AND model='reversal' "
            "GROUP BY signaled, gate_reason ORDER BY signaled DESC, n DESC LIMIT 40"
        ),
        s.get_bind(),
        params={"r": RUN},
    )
print("=== gate_reason 取值分布（按 signaled 分组）===")
print(df.to_string(index=False))

with session_scope() as s:
    smp = pd.read_sql(
        text(
            "SELECT symbol, eval_date, pred_dir, actual, signaled, gate_reason "
            "FROM backtest_detail WHERE run_id=:r AND model='reversal' AND signaled "
            "ORDER BY symbol, eval_date LIMIT 10"
        ),
        s.get_bind(),
        params={"r": RUN},
    )
print("\n=== signaled=True 抽样 10 行 ===")
print(smp.to_string(index=False))
