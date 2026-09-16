"""定位 reversal 0.5000 bug：把平台回测的 reversal 已发信号行 JOIN daily_bar，
验证 (a) 模型输出方向 pred_dir 是否 = -sign(当日收益)；(b) 在 daily_bar 口径下
这些信号日次日反转准确率（独立复算）是多少。只读。
"""
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text
from app.core.db import session_scope

sys.path.insert(0, "/app")
RUN = "20260909_171921_bt"


def main():
    with session_scope() as s:
        # 平台 reversal 已发信号行
        rev = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual FROM backtest_detail "
            "WHERE run_id=:rid AND model='reversal' AND signaled=TRUE"
        ), s.get_bind(), params={"rid": RUN})
        print(f"平台 reversal 已发信号行: {len(rev)}")

        # daily_bar 各品种收益序列（与平台非FG/SA品种同源；FG/SA用csv_smooth会有偏差，单独标注）
        db = pd.read_sql(text(
            "SELECT symbol, trade_date, close FROM daily_bar WHERE symbol LIKE '%%888' "
            "AND close IS NOT NULL ORDER BY symbol, trade_date"
        ), s.get_bind())
        db["trade_date"] = pd.to_datetime(db["trade_date"])
        db["ret_t"] = db.groupby("symbol")["close"].pct_change() * 100
        db["fwd"] = db.groupby("symbol")["ret_t"].shift(-1)
        db_map = {(r.symbol, r.trade_date): r for r in db.itertuples()}

        checked = 0
        dir_logic_ok = 0          # pred_dir == -sign(ret_t)
        rev_correct_tomorrow = 0  # -sign(ret_t) == sign(fwd)
        pred_vs_tomorrow = 0      # pred_dir == sign(fwd)  (即平台预测对次日)
        fg_sa = 0
        rows = []
        for r in rev.itertuples():
            key = (r.symbol, pd.to_datetime(r.eval_date))
            d = db_map.get(key)
            if d is None or (d.ret_t is None) or (d.fwd is None) or np.isnan(d.ret_t) or np.isnan(d.fwd):
                continue
            checked += 1
            if r.symbol in ("FG888", "SA888"):
                fg_sa += 1
            # 正确反转方向定义
            correct_rev_dir = "down" if d.ret_t > 0 else "up"
            if r.pred_dir == correct_rev_dir:
                dir_logic_ok += 1
            # 次日反转是否命中（用 daily_bar fwd）
            if np.sign(d.fwd) == np.sign(d.ret_t) * -1:
                rev_correct_tomorrow += 1
            # 平台预测方向 vs 次日实际
            if (r.pred_dir == "up" and d.fwd > 0) or (r.pred_dir == "down" and d.fwd < 0):
                pred_vs_tomorrow += 1

        print(f"可 JOIN daily_bar 校验: {checked} 行 (其中 FG/SA={fg_sa}, 用csv_smooth口径会略有偏差)")
        print(f"  [模型方向逻辑] pred_dir == -sign(当日收益) 占比 = {dir_logic_ok/checked:.4f}")
        print(f"  [真实反转] 当日收益符号 != 次日收益符号 占比 = {rev_correct_tomorrow/checked:.4f}  (应≈0.526)")
        print(f"  [平台实际命中] pred_dir == sign(次日收益) 占比 = {pred_vs_tomorrow/checked:.4f}  (平台回测称0.5000)")
        print()
        print("判读:")
        print("  - 若『模型方向逻辑』≈1.0 但『平台实际命中』≈0.50 → 模型方向对，是引擎/actual口径或计分问题")
        print("  - 若『模型方向逻辑』<1.0 → reversal 模型输出方向本身有误")
        print("  - 『真实反转』≈0.526 说明 daily_bar 口径下该信号集确有 >0.5 反转力，复算可靠")


if __name__ == "__main__":
    main()
