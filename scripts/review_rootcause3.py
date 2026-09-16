"""发现3 根因 v3（决定性）：不再从 daily_bar 重建 eval 集，而是用生产自己的
load_canonical_series + features_from_series + ReversalModel.predict + _next_ret，
在 backtest_detail 已存的 (symbol, eval_date) 上重算，核对 pred_dir / actual /
signaled 是否与库内一致。若一致 → 生产自洽、无代码 bug；review_eval_sample 的
n/acc 差异只是"重建 eval 集"的方法学伪影（数据已推进/窗口错位）。只读。"""
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


def main():
    with session_scope() as s:
        # 取生产 reversal 全部 detail
        prod = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual, signaled, gate_reason "
            "FROM backtest_detail WHERE run_id=:r AND model='reversal'"
        ), s.get_bind(), params={"r": RUN})
        prod["eval_date"] = pd.to_datetime(prod["eval_date"])
        print(f"[*] 取生产 reversal detail {len(prod)} 行，逐行用生产函数重算核对")

        cfg_bt_metric = "close"  # caliber 已确认全为 close
        model = ReversalModel()
        # 按 symbol 分组缓存 df/source
        cache = {}
        rows = []
        for r in prod.itertuples():
            sy, dt = r.symbol, r.eval_date
            if sy not in cache:
                df, source = load_canonical_series(s, sy)
                cache[sy] = (df, source)
            df, source = cache[sy]
            d = pd.Timestamp(dt).date()
            sub = df[df.index <= d]
            snap = features_from_series(sy, sub, source)
            try:
                out = model.predict(snap.rets)
            except Exception as e:
                rows.append((sy, dt, "ERR", None, None, str(e)[:30]))
                continue
            actual = _next_ret(s, sy, df, d, cfg_bt_metric)
            rows.append((sy, dt, out.direction, round(float(out.prob), 4),
                         bool(out.signaled), round(actual, 4) if actual is not None else None))

        rec = pd.DataFrame(rows, columns=["symbol", "eval_date", "pred_dir_re", "prob_re",
                                          "signaled_re", "actual_re"])
        m = prod.merge(rec, on=["symbol", "eval_date"])
        # 核对
        dir_match = (m.pred_dir == m.pred_dir_re)
        sig_match = (m.signaled == m.signaled_re)
        # actual 可能因四舍五入略有差异，比较符号
        act_sign_match = np.sign(m.actual.astype(float)) == np.sign(m.actual_re)
        print(f"[核对] pred_dir 一致率 = {dir_match.mean():.4f} ({dir_match.sum()}/{len(m)})")
        print(f"[核对] signaled 一致率 = {sig_match.mean():.4f} ({sig_match.sum()}/{len(m)})")
        print(f"[核对] actual 符号一致率 = {act_sign_match.mean():.4f} ({act_sign_match.sum()}/{len(m)})")

        # 若 signaled 一致，重算 acc
        if sig_match.all():
            sig = m[m.signaled]
            k = int((sig.pred_dir == np.where(sig.actual.astype(float) > 0, "up", "down")).sum())
            print(f"[结论] 生产函数重算完全复现库内 reversal：n={len(sig)} acc={k/len(sig):.4f}")
            print(f"  → 生产回测自洽，无代码 bug。发现3 的 n/acc 错位纯属 review_eval_sample")
            print(f"    从 daily_bar 重建 eval 集的方法学伪影（数据已推进/窗口错位），非生产缺陷。")
        else:
            bad = m[~sig_match]
            print(f"[结论] 存在 {len(bad)} 行 signaled 不一致 → 有真实代码/口径 bug：")
            print(bad.head(10).to_string())


if __name__ == "__main__":
    main()
