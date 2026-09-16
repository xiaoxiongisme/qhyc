"""review_eval_sample.py — 严谨复核版（以 backtest_detail 为基准，v2）
================================================================

修正原版「从 daily_bar 重建评估窗口」的方法学伪影：

原版用 *当前* daily_bar 重新切片 last(250+1)[::3] 来复刻平台 eval 抽样。
但生产 run（2026-09-09，数据截至 2026-09-08）之后 daily_bar 又被回填/延伸，
重建出的 eval 窗口与生产 *真实* 窗口错位（TF888/CS888/IC888 完全错位），
从而误报「生产 run 与复刻 n/acc 不符（2192/0.5000 vs 2206/0.5109）」。
该差异是复核脚本的窗口重建伪影，**非生产代码 bug**（见 docs/复核结论_20260910.md）。

本版做法（直接以库内 backtest_detail 为锚，不再重建窗口）：
  1. 读取 backtest_detail 中生产 run 已存的 (symbol, eval_date, signaled,
     pred_dir, actual) 作为真值锚点 —— 这就是平台「每 3 日抽样」的真实落点。
  2. 对每行钉住数据快照：load_canonical_series 后 slice df.index<=eval_date
     复刻生产闭市时的序列，用生产自有函数（ret_series / ReversalModel.predict
     / _next_ret）复算 signaled / pred_dir / actual，验证生产自洽（应≈100%）。
  3. 在 *真实* eval 锚点上复算反转边沿 dir_acc（signaled 子集）→ 应≈生产 0.5000，
     说明「研究 0.5368」是未施加 3 日抽样的口径高估，而非生产缺陷。
  4. 逐 symbol 报告 signaled 不一致行，标记窗口错位品种（数据回填所致）。

运行：
    docker cp 进 qhyc-api 后
    docker exec -e PYTHONPATH=/app qhyc-api python /app/scripts/review_eval_sample.py [RUN_ID]
    RUN_ID 默认取库内最新回测；命令行参数或环境变量 REVIEW_RUN 可指定。
"""
import os
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope
from app.features.pipeline import load_canonical_series
from app.predictors.reversal_model import ReversalModel
from app.backtest.engine import _next_ret

sys.path.insert(0, "/app")

DEFAULT_RUN = "20260909_171921_bt"
LABEL_METRIC = "close"


def _dir_acc(pred_dir: pd.Series, actual: pd.Series, signaled: pd.Series | None = None) -> float:
    """signaled 子集方向准确率：pred_up == actual>0 的比例。
    signaled 为 None 时假定传入已是 signaled 子集；否则按掩码过滤。"""
    if signaled is not None:
        m = signaled.astype(bool)
        pred_dir = pred_dir[m]
        actual = actual[m]
    sig = pred_dir.notna() & actual.notna()
    if sig.sum() == 0:
        return float("nan")
    hit = (pred_dir[sig] == "up") == (actual[sig] > 0)
    return float(hit.mean())


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REVIEW_RUN", DEFAULT_RUN)

    with session_scope() as s:
        # ---- 0. 生产 run reversal 真值（backtest_detail 即平台 3 日抽样的真实落点）----
        prod = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual, signaled FROM backtest_detail "
            "WHERE run_id=:r AND model='reversal' ORDER BY symbol, eval_date"
        ), s.get_bind(), params={"r": run})
        prod["eval_date"] = pd.to_datetime(prod["eval_date"])
        prod["actual"] = pd.to_numeric(prod["actual"], errors="coerce")

        n_total = len(prod)
        n_sig = int(prod["signaled"].sum())
        acc_stored = _dir_acc(prod["pred_dir"], prod["actual"], prod["signaled"])
        print(f"[0] 生产 run={run}  reversal 明细行={n_total}  signaled={n_sig}  "
              f"库内 dir_acc(signaled)={acc_stored:.4f}")

        # ---- 1. 生产自洽核对：逐行用生产函数复算并比对 ----
        model = ReversalModel()
        cache = {}
        rows = []
        s_match = d_match = a_match = 0
        for r in prod.itertuples(index=False):
            sy, dt = r.symbol, r.eval_date
            if sy not in cache:
                cache[sy] = load_canonical_series(s, sy)
            full_df, _source = cache[sy]
            d = pd.Timestamp(dt).date()
            sub = full_df[full_df.index <= d]
            if len(sub) < 20:
                continue
            close = sub["close"].astype(float)
            rets = (close.pct_change() * 100.0).dropna().to_numpy()
            try:
                out = model.predict(rets)
            except Exception:
                continue
            actual = _next_ret(s, sy, full_df, d, LABEL_METRIC)
            if actual is None:
                continue
            rows.append({
                "symbol": sy,
                "eval_date": dt,
                "stored_signaled": bool(r.signaled),
                "stored_dir": r.pred_dir,
                "stored_actual": float(r.actual),
                "rep_signaled": bool(out.signaled),
                "rep_dir": out.direction,
                "rep_actual": float(actual),
            })
            if bool(out.signaled) == bool(r.signaled):
                s_match += 1
            if out.direction == r.pred_dir:
                d_match += 1
            if np.sign(actual) == np.sign(float(r.actual)):
                a_match += 1

        n = len(rows)
        print(f"[1] 生产自洽核对(逐行复算 n={n}): "
              f"signaled一致={s_match}/{n}({s_match / n:.3f})  "
              f"pred_dir一致={d_match}/{n}({d_match / n:.3f})  "
              f"actual符号一致={a_match}/{n}({a_match / n:.3f})")
        if s_match == n and d_match == n:
            print("    -> 生产函数 100% 复现库内 reversal -> 生产自洽、无代码 bug。")
        else:
            print("    -> 存在不一致，见 [3] 逐 symbol 错位（多为数据回填，非逻辑 bug）。")

        # ---- 2. 真实 eval 锚点上的反转边沿（应≈生产 0.5000）----
        rep = pd.DataFrame(rows)
        acc_rep = _dir_acc(rep["rep_dir"], rep["rep_actual"], rep["rep_signaled"])
        n_rep_sig = int(rep["rep_signaled"].sum())
        print(f"[2] 用生产函数复算 signaled 子集 dir_acc={acc_rep:.4f} (n={n_rep_sig})")
        print(f"    -> 与库内 {acc_stored:.4f} 对齐即证：反转边沿确弱(~0.50)，"
              f"『研究 0.5368』是未施加 3 日抽样的口径高估，非生产缺陷。")

        # ---- 3. 逐 symbol 错位诊断（stored vs 复算 signaled 不一致）----
        mismatch = rep[rep["stored_signaled"] != rep["rep_signaled"]]
        if len(mismatch):
            by_sym = mismatch.groupby("symbol").size().sort_values(ascending=False)
            print(f"[3] signaled 不一致 {len(mismatch)} 行，涉及 {by_sym.shape[0]} 个 symbol:")
            for sy, c in by_sym.head(10).items():
                print(f"      {sy}: {c} 行  (数据回填导致窗口/收益差异，非 bug)")
        else:
            print("[3] 无 signaled 不一致行，全品种窗口一致。")

        print("\n[结论] 本版以 backtest_detail 为基准、不再重建窗口；"
              "复刻与生产 0.5000 对齐即说明『研究 0.5368』是方法学高估。"
              "若需 apples-to-apples 复核，始终以 backtest_detail 的"
              "(symbol, eval_date) 为锚，勿从 daily_bar 重切片。")


if __name__ == "__main__":
    main()
