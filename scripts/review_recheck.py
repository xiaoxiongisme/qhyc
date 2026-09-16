"""review_recheck.py — CodeBuddy 新交付（gate_pct 1.0%→2.0% 对齐）复检
只读。目标：
  1. 列出全部回测 run（品种数/模型数/明细行数/时间），定位最新全量 run。
  2. 对最新 run 复算 reversal 的 signaled_n / coverage / dir_acc(signaled)，
     并与 backtest_result.by_state 记录比对。
  3. 复算 ensemble dir_acc（signaled 全量）。
  4. 与历史 run 20260909_171921_bt（gate=1.0%）对比，验证阈值对齐效果（0.5005 → ?）。
"""
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope

sys.path.insert(0, "/app")
OLD_RUN = "20260909_171921_bt"


def _dir_acc(pred_dir, actual, signaled=None):
    if signaled is not None:
        m = signaled.astype(bool)
        pred_dir, actual = pred_dir[m], actual[m]
    ok = pred_dir.notna() & actual.notna()
    if ok.sum() == 0:
        return float("nan"), 0
    hit = (pred_dir[ok] == "up") == (actual[ok] > 0)
    return float(hit.mean()), int(ok.sum())


def main():
    with session_scope() as s:
        runs = pd.read_sql(text(
            "SELECT run_id, COUNT(DISTINCT symbol) AS n_sym, "
            "COUNT(DISTINCT model) AS n_model, COUNT(*) AS n_detail, "
            "MIN(eval_date) AS d0, MAX(eval_date) AS d1 "
            "FROM backtest_detail GROUP BY run_id ORDER BY MAX(eval_date) DESC, run_id DESC"
        ), s.get_bind())
        print("=== 全部回测 run（按 eval 末日期倒序）===")
        print(runs.to_string(index=False))

        latest = runs.iloc[0]["run_id"]
        print(f"\n最新 run = {latest}")

        for run in [r for r in [latest, OLD_RUN] if r]:
            det = pd.read_sql(text(
                "SELECT model, pred_dir, actual, signaled FROM backtest_detail "
                "WHERE run_id=:r"
            ), s.get_bind(), params={"r": run})
            if not len(det):
                print(f"\n[{run}] 无明细")
                continue
            det["actual"] = pd.to_numeric(det["actual"], errors="coerce")
            rev = det[det.model == "reversal"]
            ra, rn = _dir_acc(rev["pred_dir"], rev["actual"], rev["signaled"])
            cove = rn / len(rev) if len(rev) else float("nan")
            ens = det[det.model == "ensemble"]
            ea, en = _dir_acc(ens["pred_dir"], ens["actual"], ens["signaled"])
            print(f"\n[{run}] 明细={len(det)}")
            print(f"  reversal : signaled_n={rn}  coverage={cove:.4f}  dir_acc={ra:.4f}")
            print(f"  ensemble : n={en}  dir_acc={ea:.4f}")


if __name__ == "__main__":
    main()
