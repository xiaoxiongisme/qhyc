"""决定性对照：在平台 250 日回测窗口内，用 daily_bar 复算 reversal，
判定平台 reversal 0.5000 是「窗口真削弱」还是「生产 bug」。

只读，不写库。
"""
import sys
import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text
from app.core.db import session_scope

sys.path.insert(0, "/app")

W_START = pd.Timestamp("2024-08-22")
W_END = pd.Timestamp("2026-09-08")


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def bp(k, n, p0=0.5):
    return float(stats.binomtest(k, n, p0, alternative="two-sided").pvalue)


def main():
    with session_scope() as s:
        db = pd.read_sql(text(
            "SELECT symbol, trade_date, close, oi, ret_close FROM daily_bar "
            "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
        ), s.get_bind())
        for c in ("close", "oi", "ret_close"):
            db[c] = pd.to_numeric(db[c], errors="coerce")
        db["trade_date"] = pd.to_datetime(db["trade_date"])

        frames = []
        for sy, g in db.groupby("symbol"):
            g = g.sort_values("trade_date").copy()
            g["ret_t"] = g.close.pct_change() * 100
            g["fwd"] = g.ret_t.shift(-1)
            g["doi_t"] = g.oi.pct_change() * 100
            g["symbol"] = sy
            frames.append(g)
        pan = pd.concat(frames)
        pan = pan.dropna(subset=["ret_t", "fwd"]).query("ret_t != 0 and fwd != 0").copy()
        pan["rev_hit"] = (np.sign(pan.ret_t) != np.sign(pan.fwd)).astype(int)

        print("=" * 88)
        print(f"全面板：{pan.symbol.nunique()} 品种, {len(pan)} 行")
        print("=" * 88)

        def acc_of(df, mask, label):
            g = df[mask]
            n = len(g)
            if n < 50:
                print(f"  {label:42s} n={n:<6} 样本不足"); return
            k = int(g.rev_hit.sum())
            lo, hi = wilson(k, n)
            print(f"  {label:42s} n={n:<6} acc={k/n:.4f}  CI[{lo:.4f},{hi:.4f}]  p={bp(k,n):.2e}  cov={n/len(df):.1%}")

        for tag, sub in [("研究 TEST(>2023, 全期)", pan[pan.trade_date > pd.Timestamp("2023-12-31")]),
                         (f"平台窗口 [{W_START.date()}~{W_END.date()}]",
                          pan[(pan.trade_date >= W_START) & (pan.trade_date <= W_END)])]:
            print(f"\n### {tag}")
            acc_of(sub, sub.ret_t.abs() > 0,            "① 反转基线(无门控)")
            acc_of(sub, sub.ret_t.abs() > 1.0,         "② 平台门控 |ret|>1% (无oi)")
            acc_of(sub, (sub.ret_t.abs() > 1.0) & (sub.doi_t > 0), "③ 研究全规则 |ret|>1%∩Δoi>0")
            acc_of(sub, (sub.ret_t.abs() > 2.0),       "④ 更严 |ret|>2%")
            # 幅度分层
            q = pd.qcut(sub.ret_t.abs(), 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
            print("   幅度分层(反转acc): " + "  ".join(
                f"{lbl}={sub[q==lbl].rev_hit.mean():.3f}(n={int((q==lbl).sum())})" for lbl in ["Q1", "Q2", "Q3", "Q4", "Q5"]))


if __name__ == "__main__":
    main()
