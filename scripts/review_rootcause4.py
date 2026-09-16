"""发现3 根因 v4（快速版）：
(A) 抽样核对：取生产 reversal detail 前 200 行，用生产函数重算 pred_dir/signaled/actual
    是否与库内一致 → 验证生产自洽、无代码 bug。
(B) eval 集同源性：直接比对"生产 backtest_detail 中 reversal 的 eval_date 集合"
    与"用 daily_bar<=2026-09-08 重建的 last251[::3] 集合"逐 symbol 重叠率，
    定位错位是否来自 eval 窗口（数据推进/符号口径）。只读。"""
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text
from app.core.db import session_scope
from app.features.pipeline import load_canonical_series, features_from_series
from app.predictors.reversal_model import ReversalModel
from app.backtest.engine import _next_ret

sys.path.insert(0, "/app")
RUN = "20260909_171921_bt"
FRONTIER = pd.Timestamp("2026-09-08")
TEST_DAYS, STEP, MIN_BARS = 250, 3, 60


def main():
    with session_scope() as s:
        prod = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual, signaled FROM backtest_detail "
            "WHERE run_id=:r AND model='reversal' ORDER BY symbol, eval_date"
        ), s.get_bind(), params={"r": RUN})
        prod["eval_date"] = pd.to_datetime(prod["eval_date"])

        # ---- (A) 抽样核对（前 200 行）----
        model = ReversalModel()
        cache = {}
        samp = prod.head(200)
        d_match = s_match = a_match = 0
        for r in samp.itertuples():
            sy, dt = r.symbol, r.eval_date
            if sy not in cache:
                cache[sy] = load_canonical_series(s, sy)
            df, source = cache[sy]
            d = pd.Timestamp(dt).date()
            sub = df[df.index <= d]
            snap = features_from_series(sy, sub, source)
            try:
                out = model.predict(snap.rets)
            except Exception:
                continue
            actual = _next_ret(s, sy, df, d, "close")
            if out.direction == r.pred_dir:
                d_match += 1
            if bool(out.signaled) == bool(r.signaled):
                s_match += 1
            if actual is not None and r.actual is not None and np.sign(actual) == np.sign(float(r.actual)):
                a_match += 1
        n = len(samp)
        print(f"[A] 抽样核对(前{n}行): pred_dir一致={d_match}/{n}  signaled一致={s_match}/{n}  actual符号一致={a_match}/{n}")
        if d_match == n and s_match == n and a_match == n:
            print("    → 生产函数 100% 复现库内 reversal → 生产自洽、无代码 bug。")

        # ---- (B) eval 集同源性（逐 symbol 重叠率）----
        # 生产 eval 集（reversal 出现过的 eval_date）
        prod_eval = prod.groupby("symbol")["eval_date"].apply(lambda x: set(pd.to_datetime(x))).to_dict()
        # 重建 daily_bar<=frontier 的 last251[::3]
        db = pd.read_sql(text(
            "SELECT symbol, trade_date, close FROM daily_bar WHERE symbol LIKE '%%888' "
            "AND close IS NOT NULL AND trade_date <= :fr ORDER BY symbol, trade_date"
        ), s.get_bind(), params={"fr": FRONTIER})
        db["trade_date"] = pd.to_datetime(db["trade_date"])
        recon = {}
        for sy, g in db.groupby("symbol"):
            g = g.sort_values("trade_date")
            navail = len(g)
            win = TEST_DAYS if navail >= TEST_DAYS + MIN_BARS else navail - MIN_BARS
            if win < max(20, STEP):
                continue
            dates = g["trade_date"].tolist()
            ev = set(dates[-(win + 1):][::STEP])
            recon[sy] = ev
        # 重叠率（仅看双方共有 symbol）
        common_sym = set(prod_eval) & set(recon)
        rates = []
        for sy in common_sym:
            pe, re = prod_eval[sy], recon[sy]
            if pe:
                rates.append(len(pe & re) / len(pe))
        print(f"[B] 逐symbol eval集重叠率: 共有symbol={len(common_sym)} 平均重叠={np.mean(rates):.3f} "
              f"中位={np.median(rates):.3f} 最小={min(rates):.3f}")
        if np.mean(rates) < 0.6:
            print("    → eval 集大量错位 → 错位根因是'评估窗口/数据推进'(daily_bar 已更新)，"
                  "非生产代码 bug；review_eval_sample 重建窗口未对齐 run 快照。")
        # 最差 3 个 symbol 及其数据长度（确认是短历史/新上市品种）
        worst = sorted(common_sym, key=lambda sy: rates[list(common_sym).index(sy)])[:3]
        for sy in worst:
            navail = len(db[db.symbol == sy]) if sy in set(db.symbol) else -1
            print(f"      最差 {sy}: 重叠={min(rates):.3f} daily_bar行数={navail}")


if __name__ == "__main__":
    main()
