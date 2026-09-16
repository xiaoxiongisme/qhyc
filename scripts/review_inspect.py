"""抽查 reversal 已发信号行的原始数值，定位 ret_t 幅度为何与 daily_bar 对不上。
只读。"""
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text
from app.core.db import session_scope

sys.path.insert(0, "/app")
RUN = "20260909_171921_bt"


def main():
    with session_scope() as s:
        rev = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual, gate_reason FROM backtest_detail "
            "WHERE run_id=:rid AND model='reversal' AND signaled=TRUE ORDER BY symbol LIMIT 14"
        ), s.get_bind(), params={"rid": RUN})
        db = pd.read_sql(text(
            "SELECT symbol, trade_date, close, ret_close FROM daily_bar WHERE symbol LIKE '%%888' "
            "AND close IS NOT NULL"
        ), s.get_bind())
        db["trade_date"] = pd.to_datetime(db["trade_date"])
        dbmap = {}
        for sy, g in db.groupby("symbol"):
            g = g.sort_values("trade_date").copy()
            g["ret_close_t"] = g["ret_close"]
            g["close_t"] = g["close"].pct_change() * 100
            for r in g.itertuples():
                dbmap[(sy, r.trade_date)] = r

        print(f"{'sym':6} {'eval_date':10} {'pred':4} {'actual':>8} | {'db|ret_close|':>11} {'db|close%|':>9}  daily_bar大波动?")
        print("-" * 86)
        for r in rev.itertuples():
            d0 = dbmap.get((r.symbol, pd.to_datetime(r.eval_date)))
            if not d0:
                print(f"{r.symbol:6} {str(r.eval_date)[:10]:10} {r.pred_dir:4} {float(r.actual):8.3f} |  (no db row)"); continue
            rc = d0.ret_close_t
            ct = d0.close_t
            big = "YES" if (abs(rc) > 1.0 or abs(ct) > 1.0) else "no"
            print(f"{r.symbol:6} {str(r.eval_date)[:10]:10} {r.pred_dir:4} {float(r.actual):8.3f} | "
                  f"{abs(rc) if rc==rc else float('nan'):11.3f} {abs(ct) if ct==ct else float('nan'):9.3f}  {big}")


if __name__ == "__main__":
    main()
