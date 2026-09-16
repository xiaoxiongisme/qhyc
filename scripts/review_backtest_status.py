"""只读复核：核对 qhyc 回测库内实际数字 vs 文档声明。
不写任何表。运行：docker cp 进 qhyc-api 后 `python scripts/review_backtest_status.py`
"""
import sys
import pandas as pd
import numpy as np
from sqlalchemy import text
from app.core.db import session_scope


def runs_summary(s):
    q = text("""
        SELECT run_id,
               COUNT(DISTINCT symbol)                          AS symbols,
               COUNT(DISTINCT model)                           AS models,
               MIN(eval_date)                                 AS first_eval,
               MAX(eval_date)                                 AS last_eval,
               COUNT(*)                                       AS detail_rows
        FROM backtest_detail
        GROUP BY run_id
        ORDER BY last_eval DESC, run_id DESC
    """)
    return pd.read_sql(q, s.bind)


def model_acc(s, run_id):
    """跨品种聚合 dir_acc；同时给出全量口径与 signaled-only 口径，以及覆盖率。"""
    q = text("""
        SELECT model,
               signaled,
               pred_dir,
               CASE WHEN actual > 0 THEN 'up' WHEN actual < 0 THEN 'down' ELSE 'flat' END AS act_dir,
               COUNT(*) AS n
        FROM backtest_detail
        WHERE run_id = :rid
        GROUP BY model, signaled, pred_dir, act_dir
    """)
    df = pd.read_sql(q, s.bind, params={"rid": run_id})

    def acc_of(g):
        n = int(g["n"].sum())
        corr = int(g.loc[(g["pred_dir"] == g["act_dir"]) &
                         (g["act_dir"] != "flat"), "n"].sum())
        flats = int(g.loc[g["act_dir"] == "flat", "n"].sum())
        nonflat = n - flats
        return (corr / nonflat if nonflat else float("nan")), n, flats

    rows = []
    for model, g in df.groupby("model"):
        acc_all, n, flats = acc_of(g)
        g_sig = g[g["signaled"] == True]
        acc_sig, sig_n, _ = acc_of(g_sig) if len(g_sig) else (float("nan"), 0, 0)
        rows.append({
            "model": model,
            "n": n,
            "flat": flats,
            "dir_acc_all": round(acc_all, 4),
            "dir_acc_signaled": round(acc_sig, 4),
            "coverage": round(sig_n / n, 4) if n else float("nan"),
            "signaled_n": int(sig_n),
        })
    out = pd.DataFrame(rows).sort_values("dir_acc_all", ascending=False)
    return out


def main():
    with session_scope() as s:
        print("=" * 90)
        print("A. 全部回测 run 概况")
        print("=" * 90)
        rs = runs_summary(s)
        print(rs.to_string(index=False))

        # 选最新且品种数最多的 run 作为主复核对象
        top = rs.sort_values(["symbols", "last_eval"], ascending=False).iloc[0]
        rid = top["run_id"]
        print("\n" + "=" * 90)
        print(f"B. 主复核 run = {rid}  (symbols={top['symbols']}, "
              f"eval {top['first_eval']} ~ {top['last_eval']}, rows={top['detail_rows']})")
        print("=" * 90)
        acc = model_acc(s, rid)
        print(acc.to_string(index=False))

        # ensemble / reversal 重点核对
        print("\n--- 重点：ensemble vs reversal（all=含未发信号；signaled=仅发信号子集）---")
        for m in ["ensemble", "reversal"]:
            r = acc[acc["model"] == m]
            if not r.empty:
                r0 = r.iloc[0]
                flag = "✅≥0.53" if (m == "reversal" and r0["dir_acc_signaled"] >= 0.53) else "⚠️<0.53"
                print(f"  {m:10s} dir_acc_all={r0['dir_acc_all']:.4f}  "
                      f"dir_acc_signaled={r0['dir_acc_signaled']:.4f}  "
                      f"n={int(r0['n'])}  coverage={r0['coverage']:.3f}  "
                      f"signaled_n={int(r0['signaled_n'])}  {flag}")

        # reversal gate_reason 分布
        print("\n--- reversal 门控原因分布 ---")
        gq = text("""
            SELECT gate_reason, COUNT(*) AS n
            FROM backtest_detail
            WHERE run_id = :rid AND model = 'reversal'
            GROUP BY gate_reason ORDER BY n DESC
        """)
        print(pd.read_sql(gq, s.bind, params={"rid": rid}).to_string(index=False))

        # 池化模型是否存在于本 run
        pooled = [m for m in acc["model"].tolist() if "pool" in m]
        print(f"\n--- 池化模型在本 run 中：{pooled if pooled else '（未参与该 run）'} ---")


if __name__ == "__main__":
    main()
