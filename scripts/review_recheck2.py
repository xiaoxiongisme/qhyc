"""review_recheck2.py — 深挖新 run 的 reversal 聚合：result(by_state) vs detail
只读。目标：定位 CodeBuddy 声称 0.5281(n=5544) 与我复算 0.5492(n=914) 的差异来源。
"""
import sys
import json
import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text

from app.core.db import session_scope

sys.path.insert(0, "/app")
RUN = "20260910_143938_bt"
OLD = "20260909_171921_bt"


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def main():
    with session_scope() as s:
        # ---- 1. 新 run 的模型清单 + 每模型明细池化 dir_acc ----
        det = pd.read_sql(text(
            "SELECT model, symbol, pred_dir, actual, signaled FROM backtest_detail "
            "WHERE run_id=:r"
        ), s.get_bind(), params={"r": RUN})
        det["actual"] = pd.to_numeric(det["actual"], errors="coerce")
        print(f"=== run {RUN} 明细 {len(det)} 行，模型 {det.model.nunique()} 个 ===")
        print("模型:", sorted(det.model.unique()))

        rev = det[det.model == "reversal"]
        sig = rev[rev.signaled.astype(bool)]
        k = int(((sig.pred_dir == "up") == (sig.actual > 0)).sum())
        n = len(sig)
        lo, hi = wilson(k, n)
        p = stats.binomtest(k, n, 0.5, alternative="two-sided").pvalue
        print(f"\n[detail 池化] reversal signaled n={n}  hit={k}  dir_acc={k/n:.4f}  "
              f"Wilson[{lo:.4f},{hi:.4f}]  p={p:.2e}")

        # 逐 symbol
        per = []
        for sy, g in sig.groupby("symbol"):
            kk = int(((g.pred_dir == "up") == (g.actual > 0)).sum())
            per.append((sy, len(g), kk / len(g) if len(g) else float("nan")))
        pdf = pd.DataFrame(per, columns=["symbol", "n", "acc"])
        print(f"  逐symbol: n_symbols={len(pdf)}  macro(等权)acc={pdf.acc.mean():.4f}  "
              f"signaled_n 合计={pdf.n.sum()}")

        # ---- 2. result(by_state) 侧 ----
        res = pd.read_sql(text(
            "SELECT symbol, dir_acc, by_state FROM backtest_result "
            "WHERE run_id=:r AND model='reversal'"
        ), s.get_bind(), params={"r": RUN})
        rows = []
        for _, r in res.iterrows():
            bs = r["by_state"] if isinstance(r["by_state"], dict) else (
                json.loads(r["by_state"]) if r["by_state"] else {})
            rows.append((r["symbol"], r["dir_acc"], bs.get("signaled_n"),
                         bs.get("n"), bs))
        rdf = pd.DataFrame(rows, columns=["symbol", "dir_acc", "sig_n", "n_all", "bs"])
        rdf["sig_n"] = pd.to_numeric(rdf.sig_n, errors="coerce")
        print(f"\n[result by_state] 行数={len(rdf)}  signaled_n 合计={int(rdf.sig_n.fillna(0).sum())}")
        w = rdf.dropna(subset=["dir_acc", "sig_n"])
        wt = (w.dir_acc * w.sig_n).sum() / w.sig_n.sum()
        print(f"  按 signaled_n 加权 dir_acc={wt:.4f}  宏平均={w.dir_acc.mean():.4f}")
        print(f"  抽样 by_state keys: {list(rdf.bs.iloc[0].keys()) if len(rdf) else '-'}")

        # ---- 3. 与 n=5544 对照：可能是 n_all 合计？----
        print(f"\n  n_all(合计)={int(pd.to_numeric(rdf.n_all, errors='coerce').fillna(0).sum())}  "
              f"signaled_n(合计)={int(rdf.sig_n.fillna(0).sum())}  → 对照 CodeBuddy n=5544")

        # ---- 4. 旧 run 对照 ----
        det0 = pd.read_sql(text(
            "SELECT pred_dir, actual, signaled FROM backtest_detail "
            "WHERE run_id=:r AND model='reversal'"
        ), s.get_bind(), params={"r": OLD})
        det0["actual"] = pd.to_numeric(det0["actual"], errors="coerce")
        s0 = det0[det0.signaled.astype(bool)]
        k0 = int(((s0.pred_dir == "up") == (s0.actual > 0)).sum())
        print(f"\n[旧 run gate=1.0%] reversal signaled n={len(s0)} acc={k0/len(s0):.4f}")


if __name__ == "__main__":
    main()
