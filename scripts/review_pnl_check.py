"""只读：核对"净 P&L"口径（生产公式 sign*acts - 2*cost），
对比【池化（全信号行）】与【宏平均（逐品种 mean 再平均）】两种口径，
消除此前 0.028% 与 0.198% 的数字冲突。"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope

RUN = "20260910_143938_bt"
cost = float(get_settings().yaml.backtest.cost_pct)

print(f"=== cost 参数 ===\n  backtest.cost_pct = {cost}  →  每笔扣 2×cost = {2*cost}")

with session_scope() as s:
    det = pd.read_sql(
        text(
            "SELECT symbol, pred_dir, actual, signaled FROM backtest_detail "
            "WHERE run_id=:r AND model='reversal'"
        ),
        s.get_bind(),
        params={"r": RUN},
    )
    res = pd.read_sql(
        text(
            "SELECT symbol, by_state FROM backtest_result "
            "WHERE run_id=:r AND model='reversal'"
        ),
        s.get_bind(),
        params={"r": RUN},
    )

det["actual"] = pd.to_numeric(det["actual"], errors="coerce")
sg = det[det["signaled"].fillna(False)].copy()
sg["sign"] = np.where(sg["pred_dir"] == "up", 1.0, -1.0)
sg["pnl"] = sg["sign"] * sg["actual"] - 2 * cost

print("\n=== [A] 池化口径（全部信号行等权）===")
print(f"  信号数 n={len(sg)}  毛P&L/笔={float((sg['sign']*sg['actual']).mean()):+.4f}%  "
      f"净P&L/笔={float(sg['pnl'].mean()):+.4f}%")
print(f"  累计净={float(sg['pnl'].sum()):+.2f}%  胜率(pnl>0)={float((sg['pnl']>0).mean()):.4f}")

print("\n=== [B] 宏平均口径（逐品种 mean 再平均，生产 by_state 的算法）===")
per = sg.groupby("symbol")["pnl"].mean()
print(f"  品种数={len(per)}  宏平均净P&L/笔={float(per.mean()):+.4f}%  "
      f"中位={float(per.median()):+.4f}%  正品种={int((per>0).sum())}/{len(per)}")

print("\n=== [C] 生产 by_state 存的 net_pnl_mean（应≈宏平均）===")
vals = []
for _, r in res.iterrows():
    bs = r["by_state"] if isinstance(r["by_state"], dict) else json.loads(r["by_state"])
    v = bs.get("net_pnl_mean")
    if v is not None:
        vals.append(float(v))
v = pd.Series(vals)
print(f"  记录数={len(v)}  均值={v.mean():+.4f}%  中位={v.median():+.4f}%  "
      f"min={v.min():+.4f}%  max={v.max():+.4f}%")

print("\n=== 结论 ===")
print(f"  池化净/笔 = {float(sg['pnl'].mean()):+.4f}% ；宏平均净/笔 = {float(per.mean()):+.4f}%")
print("  两者差异来源：宏平均给每个品种等权（含信号极少的品种），池化按信号数加权。")
