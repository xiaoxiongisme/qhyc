"""验证 weights.py 的 updated_at 刷新修复（2026-09-10 复核配套）

- 对指定 run 幂等重算权重（值不变），确认 model_weights.updated_at 被刷新。
- 只读 + 幂等重算，不改变权重数值。
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from app.backtest.weights import update_model_weights
from app.core.db import session_scope

RUN = "20260910_143938_bt"


def dump(s, tag: str) -> pd.DataFrame:
    df = pd.read_sql(
        text(
            "SELECT model, weight, dir_acc, sample_n, source, updated_at "
            "FROM model_weights ORDER BY model"
        ),
        s.get_bind(),
    )
    print(f"--- {tag} ---")
    print(df.to_string(index=False))
    return df


with session_scope() as s:
    before = dump(s, "BEFORE 重算")

    res = update_model_weights(s, run_id=RUN)
    print(f"\n[update_model_weights] {res}\n")

with session_scope() as s:
    after = dump(s, "AFTER 重算")

# ---- 校验 ----
# 期望刷新集 = 本 run 实际产出的模型（排除 ensemble，与 update_model_weights 一致）
with session_scope() as s:
    in_run = {
        r[0]
        for r in s.execute(
            text(
                "SELECT DISTINCT model FROM backtest_result "
                "WHERE run_id=:r AND model <> 'ensemble'"
            ),
            {"r": RUN},
        )
    }

cmp = before.merge(after, on="model", suffixes=("_b", "_a"))
cmp["本run产出"] = cmp["model"].isin(in_run)
w_same = (
    cmp["weight_b"].astype(float).round(4) == cmp["weight_a"].astype(float).round(4)
).all()
refreshed = cmp[cmp["本run产出"]]["updated_at_b"] != cmp[cmp["本run产出"]]["updated_at_a"]
inert = cmp[~cmp["本run产出"]]

print("\n=== 校验 ===")
print(f"权重数值是否完全不变               : {w_same}")
print(f"本 run 产出模型数                  : {int(cmp['本run产出'].sum())}")
print(f"其中 updated_at 已刷新             : {int(refreshed.sum())} / {len(refreshed)}")
if len(inert):
    print(f"不在本 run 的历史残留行（保持旧值）: {list(inert['model'])}")
ok = w_same and refreshed.all() and len(refreshed) > 0
print(
    "结论:",
    "✅ 修复生效（本 run 产出的模型 updated_at 全部随更新刷新，权重数值不变）"
    if ok
    else "❌ 异常，请检查",
)
