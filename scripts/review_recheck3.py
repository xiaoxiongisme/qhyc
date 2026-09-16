"""review_recheck3.py — 新 run 反转口径全景 + 权重来源判定 + 生产自洽校验
只读。回答：
  A. 新 run(gate=2.0) / 旧 run(gate=1.0) 的反转在不同口径下的 dir_acc：
     - 池化(明细 signaled 加权)  /  宏平均(per-symbol 等权)
     - sample_n 加权(整体 dir_acc)  /  sample_n 加权(recent60_dir_acc，即权重输入)
  B. 判定 model_weights.reversal=0.5244 究竟来自哪个 run / 哪种口径。
  C. 新 run 生产自洽：用生产函数复算 signaled/pred_dir/actual，验证 100%。
"""
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope
from app.features.pipeline import load_canonical_series
from app.predictors.reversal_model import ReversalModel
from app.backtest.engine import _next_ret

sys.path.insert(0, "/app")
NEW = "20260910_143938_bt"
OLD = "20260909_171921_bt"


def w_formula(acc):
    return min(2.0, max(0.1, (acc - 0.5) * 8 + 0.3))


def summarize(s, run):
    res = pd.read_sql(text(
        "SELECT symbol, dir_acc, sample_n, by_state FROM backtest_result "
        "WHERE run_id=:r AND model='reversal'"
    ), s.get_bind(), params={"r": run})
    rows = []
    for _, r in res.iterrows():
        bs = r["by_state"] if isinstance(r["by_state"], dict) else {}
        rows.append((r["symbol"], r["dir_acc"], r["sample_n"], bs.get("recent60_dir_acc"),
                     bs.get("signaled_n")))
    d = pd.DataFrame(rows, columns=["symbol", "dir_acc", "sample_n", "recent60", "sig_n"])
    for c in ("dir_acc", "sample_n", "recent60", "sig_n"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    det = pd.read_sql(text(
        "SELECT pred_dir, actual, signaled FROM backtest_detail "
        "WHERE run_id=:r AND model='reversal'"
    ), s.get_bind(), params={"r": run})
    det["actual"] = pd.to_numeric(det["actual"], errors="coerce")
    sg = det[det.signaled.astype(bool)]
    pool_acc = ((sg.pred_dir == "up") == (sg.actual > 0)).mean()
    print(f"\n===== {run} =====")
    print(f"  明细: 总行={len(det)}  signaled={len(sg)}  coverage={len(sg)/len(det):.4f}")
    print(f"  A1 池化(明细 signaled) dir_acc = {pool_acc:.4f}")
    print(f"  A2 宏平均(per-symbol 等权) dir_acc = {d.dir_acc.mean():.4f}")
    nn = d.dropna(subset=["dir_acc", "sample_n"]).query("sample_n>0")
    print(f"  A3 sample_n 加权(整体 dir_acc) = {(nn.dir_acc*nn.sample_n).sum()/nn.sample_n.sum():.4f}"
          f"   (total_n={int(nn.sample_n.sum())})")
    mm = d.dropna(subset=["recent60", "sample_n"]).query("sample_n>0")
    r60 = (mm.recent60*mm.sample_n).sum()/mm.sample_n.sum()
    print(f"  A4 sample_n 加权(recent60，权重输入) = {r60:.4f}   (total_n={int(mm.sample_n.sum())})"
          f"  -> 隐含 w={w_formula(r60):.4f}")
    print(f"     per-symbol sample_n: 唯一值={sorted(d.sample_n.dropna().unique())[:6]}... "
          f"合计={int(d.sample_n.fillna(0).sum())}  (注意=total eval points，非 signaled)")


def main():
    with session_scope() as s:
        summarize(s, NEW)
        summarize(s, OLD)

        mw = pd.read_sql(text(
            "SELECT model, weight, dir_acc, sample_n, updated_at FROM model_weights "
            "WHERE model='reversal'"
        ), s.get_bind())
        print("\n===== model_weights.reversal 落库 =====")
        print(mw.to_string(index=False))

        # ---- C. 新 run 生产自洽（全量复算）----
        prod = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual, signaled FROM backtest_detail "
            "WHERE run_id=:r AND model='reversal'"
        ), s.get_bind(), params={"r": NEW})
        prod["eval_date"] = pd.to_datetime(prod["eval_date"])
        model, cache = ReversalModel(), {}
        sm = dm = am = n = 0
        for r in prod.itertuples(index=False):
            sy, dt = r.symbol, r.eval_date
            if sy not in cache:
                cache[sy] = load_canonical_series(s, sy)
            full, _ = cache[sy]
            dd = pd.Timestamp(dt).date()
            sub = full[full.index <= dd]
            if len(sub) < 20:
                continue
            rets = (sub["close"].astype(float).pct_change() * 100).dropna().to_numpy()
            try:
                out = model.predict(rets)
            except Exception:
                continue
            act = _next_ret(s, sy, full, dd, "close")
            if act is None:
                continue
            n += 1
            sm += bool(out.signaled) == bool(r.signaled)
            dm += out.direction == r.pred_dir
            am += np.sign(act) == np.sign(float(r.actual))
        print(f"\n===== 新 run 生产自洽（全量复算 n={n}）=====")
        print(f"  signaled 一致={sm}/{n}  pred_dir 一致={dm}/{n}  actual 符号一致={am}/{n}")


if __name__ == "__main__":
    main()
