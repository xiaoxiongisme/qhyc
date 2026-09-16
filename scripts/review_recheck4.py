"""review_recheck4.py — 新 run 各模型池化 dir_acc + 反转净P&L（验收③）。只读。"""
import sys
import json
import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text

from app.core.db import session_scope

sys.path.insert(0, "/app")
RUN = "20260910_143938_bt"


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


with session_scope() as s:
    det = pd.read_sql(text(
        "SELECT model, pred_dir, actual, signaled FROM backtest_detail WHERE run_id=:r"
    ), s.get_bind(), params={"r": RUN})
    det["actual"] = pd.to_numeric(det["actual"], errors="coerce")
    print(f"=== {RUN} 各模型（signaled 池化）===")
    print(f"{'model':<12}{'n':>7}{'dir_acc':>10}{'Wilson':>20}{'p':>12}")
    for m, g in det.groupby("model"):
        gg = g[g.signaled.astype(bool)]
        n = len(gg)
        if n == 0:
            continue
        k = int(((gg.pred_dir == "up") == (gg.actual > 0)).sum())
        lo, hi = wilson(k, n)
        p = stats.binomtest(k, n, 0.5, alternative="two-sided").pvalue
        print(f"{m:<12}{n:>7}{k/n:>10.4f}   [{lo:.4f},{hi:.4f}]{p:>11.2e}")

    res = pd.read_sql(text(
        "SELECT by_state FROM backtest_result WHERE run_id=:r AND model='reversal'"
    ), s.get_bind(), params={"r": RUN})
    agg = {}
    for _, r in res.iterrows():
        bs = r["by_state"] if isinstance(r["by_state"], dict) else json.loads(r["by_state"])
        for kk in ("net_pnl_mean", "net_pnl_total", "win_rate_pnl", "coverage", "cost_pct"):
            v = bs.get(kk)
            if v is not None:
                agg.setdefault(kk, []).append(v)
    print("\n=== 反转 by_state 汇总（验收③：含成本净P&L>0）===")
    for kk, vs in agg.items():
        vs = pd.to_numeric(pd.Series(vs), errors="coerce").dropna()
        print(f"  {kk}: 均值={vs.mean():.5f}  min={vs.min():.5f}  max={vs.max():.5f}  (n={len(vs)})")
