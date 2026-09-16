"""确认根因：用 daily_bar.ret_close（结算/调整口径）复算 reversal，
对比 close.pct_change（收盘价口径）。若 ret_close 口径 ≈0.49 而 close≈0.526，
则证明平台 reversal 用了错的价格序列。只读。"""
import sys
import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text
from app.core.db import session_scope

sys.path.insert(0, "/app")
WS, WE = pd.Timestamp("2024-08-22"), pd.Timestamp("2026-09-08")


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def bp(k, n):
    return float(stats.binomtest(k, n, 0.5, alternative="two-sided").pvalue)


def main():
    with session_scope() as s:
        db = pd.read_sql(text(
            "SELECT symbol, trade_date, close, ret_close FROM daily_bar "
            "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
        ), s.get_bind())
        for c in ("close", "ret_close"):
            db[c] = pd.to_numeric(db[c], errors="coerce")
        db["trade_date"] = pd.to_datetime(db["trade_date"])

        frames = []
        for sy, g in db.groupby("symbol"):
            g = g.sort_values("trade_date").copy()
            g["ret_close_t"] = g["ret_close"]                     # 结算口径当日收益
            g["close_t"] = g["close"].pct_change() * 100          # 收盘价口径
            g["fwd_close"] = g["close_t"].shift(-1)
            g["fwd_rc"] = g["ret_close"].shift(-1)
            g["symbol"] = sy
            frames.append(g)
        pan = pd.concat(frames)
        pan = pan.dropna(subset=["ret_close_t", "fwd_rc", "close_t", "fwd_close"])
        pan = pan[(pan.trade_date >= WS) & (pan.trade_date <= WE)]

        def acc(df, rt, fwd, thr, lab):
            m = df[rt].abs() > thr
            g = df[m]
            n = len(g); k = int(((np.sign(g[rt]) != np.sign(g[fwd]))).sum())
            lo, hi = wilson(k, n)
            print(f"  {lab:34s} n={n:<6} acc={k/n:.4f} CI[{lo:.4f},{hi:.4f}] p={bp(k,n):.2e}")

        print("平台窗口内，两种价格序列的 reversal（门控 |ret|>1%）：")
        acc(pan, "ret_close_t", "fwd_rc", 1.0, "① ret_close 口径(结算/调整)")
        acc(pan, "close_t", "fwd_close", 1.0, "② close.pct_change 口径(收盘价)")
        print("\n判读：若①≈0.49 而②≈0.526 → 平台用了 ret_close 序列，需改为收盘价口径。")


if __name__ == "__main__":
    main()
