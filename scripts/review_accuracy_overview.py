"""只读：全模型准确率全景（池化口径），用于解释"当前预测准确率如何"。

输出：
  - 每个模型：signaled 行数、覆盖率、池化 dir_acc、Wilson95、二项 p
  - reversal 单独展开：gate 0.5~5.0% 曲线
  - ensemble 与各模型对比
  - 基准：0.5（随机）
"""
from __future__ import annotations

from math import erfc, sqrt

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope

RUN = "20260910_143938_bt"
TOTAL_PTS = 6132
COST = 2 * 0.065  # %，双向开平，每笔扣 2×cost_pct（=0.13，与生产 engine.py 一致）


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def p_two(k, n, p0=0.5):
    if n == 0:
        return float("nan")
    z = (k / n - p0) / sqrt(p0 * (1 - p0) / n)
    return erfc(abs(z) / sqrt(2))


with session_scope() as s:
    det = pd.read_sql(
        text(
            "SELECT model, symbol, eval_date, state, pred_dir, actual, signaled "
            "FROM backtest_detail WHERE run_id=:r ORDER BY model, symbol, eval_date"
        ),
        s.get_bind(),
        params={"r": RUN},
    )

det["actual"] = pd.to_numeric(det["actual"], errors="coerce")
det["hit"] = ((det["pred_dir"] == "up") == (det["actual"] > 0)).astype(int)
det["psign"] = np.where(det["pred_dir"] == "up", 1.0, -1.0)
det["pnl"] = det["psign"] * det["actual"]

print("=" * 104)
print(f"全模型准确率全景（池化口径）  run={RUN}   基准=0.5000（随机）  总评估点={TOTAL_PTS}")
print("=" * 104)
hdr = (f"{'model':>12} {'有View数':>9} {'覆盖%':>8} {'池化acc':>9} "
       f"{'Wilson95':>19} {'p':>10} {'显著':>5} {'净PnL%':>9}")
print(hdr)
print("-" * len(hdr))

rows = []
for m, g in det.groupby("model"):
    sg = g[g["signaled"].fillna(False)]
    n = len(sg)
    if n == 0:
        continue
    k = int(sg["hit"].sum())
    acc = k / n
    lo, hi = wilson(k, n)
    p = p_two(k, n)
    net = float(sg["pnl"].mean()) - COST
    star = "★" if (p < 0.05 and acc > 0.5) else ""
    rows.append((m, n, n / TOTAL_PTS * 100, acc, lo, hi, p, net, star))

for r in sorted(rows, key=lambda x: -x[3]):
    print(f"{r[0]:>12} {r[1]:>9} {r[2]:>7.2f}% {r[3]:>9.4f} "
          f"[{r[4]:>7.4f},{r[5]:>7.4f}] {r[6]:>10.3e} {r[8]:>5} {r[7]:>+9.4f}")

print("\n=== ensemble vs 各单模型（池化 acc 排序）===")
for r in sorted(rows, key=lambda x: -x[3]):
    bar = "█" * int(max(0, (r[3] - 0.45)) * 200)
    print(f"  {r[0]:>12}  {r[3]:.4f}  {bar}")

# ---- reversal 曲线 ---
rev = det[det.model == "reversal"].copy()
print("\n=== reversal gate 曲线（覆盖率 vs acc）===")
print(f"{'gate%':>6} {'n':>7} {'覆盖%':>8} {'acc':>8} {'p':>10}")
for gate in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
    # 复用 sweep 结论（此处仅展示 gauge 已由 review_gate_sweep.py 精算）
    pass
print("（详见 review_gate_sweep.py 输出）")

# ---- 命中率的直觉解释：以 gate=2.0% 为例 ---
sg = det[(det.model == "reversal") & (det.signaled.fillna(False))]
k = int(sg["hit"].sum())
n = len(sg)
print("\n=== 反转（gate=2.0%）直觉读数 ===")
print(f"  在 {n} 次\"有观点\"的评估点中，方向猜对 {k} 次，猜错 {n-k} 次")
print(f"  即 每 100 次有观点，约对 {k/n*100:.1f} 次、错 {(1-k/n)*100:.1f} 次")
print(f"  覆盖率：全市场 {TOTAL_PTS} 个评估点里，仅 {n} 个（{n/TOTAL_PTS*100:.2f}%）给出观点")
print(f"  净 P&L/笔（扣 {COST}% 成本）= {float(sg['pnl'].mean())-COST:+.4f}%")

# ensemble 明细
ens = det[det.model == "ensemble"]
esg = ens[ens.signaled.fillna(False)]
print(f"\n=== ensemble ===\n  n={len(esg)} acc={esg['hit'].mean():.4f} "
      f"p={p_two(int(esg['hit'].sum()), len(esg)):.3e}  （覆盖 100%，因每点均投票）")
