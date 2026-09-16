"""发现3 根因排查 v2：生产 run 20260909_171921_bt(数据边界 2026-09-08) reversal
signaled n=2192/acc=0.5005，与日线复刻② n=2206/acc=0.5109 的错位根因。
v1 结论：source 不是原因(生产 71/73 也用 akshare_tqsdk=daily_bar)；
共同 signaled 日两口径 acc 均≈0.50，但 signaled 集合仅重叠 1322/2192(60%)。
假设升级：复刻②查询的是"当前"daily_bar(比 run 晚 2 天)，导致 3 日 eval 网格整体平移，
约 1/3 eval 日错位 → 集合错位 + acc 漂移。本脚本把复刻窗口钉到 run 边界 2026-09-08
并复用自适应当口逻辑，看是否与生产 2192/0.5005 吻合。只读。"""
import sys
import numpy as np
import pandas as pd
from sqlalchemy import text
from app.core.db import session_scope

sys.path.insert(0, "/app")
RUN = "20260909_171921_bt"
FRONTIER = pd.Timestamp("2026-09-08")   # 生产 run 数据边界
TEST_DAYS = 250
STEP = 3
MIN_BARS = 60


def main():
    with session_scope() as s:
        # 1. 生产 reversal 全部 detail
        prod = pd.read_sql(text(
            "SELECT symbol, eval_date, pred_dir, actual, signaled, gate_reason "
            "FROM backtest_detail WHERE run_id=:r AND model='reversal' ORDER BY symbol, eval_date"
        ), s.get_bind(), params={"r": RUN})
        prod["eval_date"] = pd.to_datetime(prod["eval_date"])
        prod_sig = prod[prod.signaled].copy()
        prod_set = set(zip(prod_sig.symbol, prod_sig.eval_date))
        print(f"[1] 生产 signaled n={len(prod_set)} (总 detail {len(prod)})")

        # 2. daily_bar 钉到 run 边界
        db = pd.read_sql(text(
            "SELECT symbol, trade_date, close FROM daily_bar "
            "WHERE symbol LIKE '%%888' AND close IS NOT NULL AND trade_date <= :fr "
            "ORDER BY symbol, trade_date"
        ), s.get_bind(), params={"fr": FRONTIER})
        db["trade_date"] = pd.to_datetime(db["trade_date"])
        db["close"] = pd.to_numeric(db["close"], errors="coerce")

        rep_rows = []
        for sy, g in db.groupby("symbol"):
            g = g.sort_values("trade_date")
            g["ret_t"] = g["close"].pct_change() * 100
            g["fwd"] = g["ret_t"].shift(-1)
            g = g.dropna(subset=["ret_t", "fwd"]).query("ret_t!=0 and fwd!=0")
            n_avail = len(g)
            window = TEST_DAYS
            if n_avail < TEST_DAYS + MIN_BARS:
                window = n_avail - MIN_BARS
                if window < max(20, STEP):
                    continue
            dates = g["trade_date"].tolist()
            ev = set(dates[-(window + 1):][::STEP])
            for _, row in g[g["trade_date"].isin(ev)].iterrows():
                if abs(row.ret_t) > 1.0:
                    pred = "down" if row.ret_t > 0 else "up"
                    hit = (pred == ("up" if row.fwd > 0 else "down"))
                    rep_rows.append((sy, row.trade_date, hit))
        rep = pd.DataFrame(rep_rows, columns=["symbol", "eval_date", "hit_r"])
        rep_set = set(zip(rep.symbol, rep.eval_date))
        print(f"[2] 钉边界复刻② signaled n={len(rep_set)} acc={rep.hit_r.mean():.4f}")

        # 3. 集合对比
        common = prod_set & rep_set
        p_only = prod_set - rep_set
        r_only = rep_set - prod_set
        print(f"[3] signaled 集合: 共同 {len(common)} / 生产独有 {len(p_only)} / 复刻独有 {len(r_only)}")
        # 共同日 acc
        rep_map = {(r.symbol, r.eval_date): bool(r.hit_r) for r in rep.itertuples()}
        prod_map = {(r.symbol, r.eval_date): (r.pred_dir, float(r.actual)) for r in prod_sig.itertuples()}
        c_hit = [prod_map[k][0] == ("up" if prod_map[k][1] > 0 else "down") for k in common]
        print(f"[4] 共同 signaled 日: 生产acc={np.mean(c_hit):.4f}(n={len(c_hit)}) "
              f"/ 复刻acc={np.mean([rep_map[k] for k in common]):.4f}(n={len(common)})")

        # 4. 残余错位按 symbol 归因（是否集中在 2 个 csv_smooth 品种）
        from collections import Counter
        po_sym = Counter(s for s, d in p_only)
        ro_sym = Counter(s for s, d in r_only)
        print(f"[5] 生产独有(P_only) 按 symbol Top5: {po_sym.most_common(5)}")
        print(f"    复刻独有(R_only) 按 symbol Top5: {ro_sym.most_common(5)}")
        # csv_smooth 品种（v1 已知 2 个）
        csv_sym = pd.read_sql(text(
            "SELECT DISTINCT symbol FROM backtest_result "
            "WHERE run_id=:r AND model='reversal' AND by_state->>'source'='csv_smooth'"
        ), s.get_bind(), params={"r": RUN})["symbol"].tolist()
        print(f"    csv_smooth 品种={csv_sym}")
        po_csv = sum(po_sym.get(x, 0) for x in csv_sym)
        ro_csv = sum(ro_sym.get(x, 0) for x in csv_sym)
        print(f"    P_only 落在 csv_smooth 品种={po_csv}  R_only 落在 csv_smooth 品种={ro_csv}")

        # 5. 结论
        if len(common) >= 0.95 * len(prod_set) and abs(len(rep_set) - len(prod_set)) <= 30:
            print("\n[结论] 钉边界后复刻与生产 signaled 集合高度吻合(n 差 "
                  f"{len(rep_set)-len(prod_set)}) → 发现3 错位根因=复刻②未钉 run 数据边界(晚2天"
                  "导致3日eval网格平移)，非生产代码bug。生产 reversal 0.5005 自洽。")
        else:
            print("\n[结论] 即便钉边界，集合仍显著错位 → 存在真实代码/口径 bug，需进一步排查。")


if __name__ == "__main__":
    main()
