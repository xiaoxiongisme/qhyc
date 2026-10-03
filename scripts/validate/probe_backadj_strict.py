# -*- coding: utf-8 -*-
"""严谨复验：用 roll_segment.roll_ts 精确定位换月点，验证真实 back_adjust 是否抵消；
并统计 price_shift 是否启用、负价真实数量。只读本地库。"""
import os, sys
sys.path.insert(0, r"E:\Docker\qhyc"); os.chdir(r"E:\Docker\qhyc")
import numpy as np, pandas as pd
from sqlalchemy import create_engine, text
from app.data.back_adjust import apply_back_adjust, check_negative

eng = create_engine("postgresql+psycopg2://futures:qhyc_dev_pwd_2026@127.0.0.1:5432/futures")

def variety_syms():
    with eng.connect() as c:
        return [r[0] for r in c.execute(text(
            "SELECT DISTINCT symbol FROM roll_segment WHERE freq='min15' ORDER BY symbol"))]

def strict_cancel(sym):
    with eng.connect() as c:
        df = pd.read_sql(text("SELECT bucket,open,high,low,close FROM bar_15m "
                              "WHERE symbol=:s ORDER BY bucket"), c, params={"s": sym})
        segs = pd.read_sql(text("SELECT seg_no,seg_start,seg_end,roll_ts,roll_delta,"
                                "cum_offset,price_shift FROM roll_segment "
                                "WHERE symbol=:s AND freq='min15' ORDER BY seg_no"),
                           c, params={"s": sym})
    if df.empty or segs.empty: return None
    out = apply_back_adjust(df.copy(), sym, "min15", session=None, segs=segs, time_col="bucket")
    bts = pd.to_datetime(df['bucket'])
    raw = df['open'].astype(float).values[1:] - df['close'].astype(float).values[:-1]
    adj = out['open'].astype(float).values[1:] - out['close'].astype(float).values[:-1]
    res = []
    for _, s in segs.iterrows():
        if pd.isna(s['roll_ts']): continue
        k = int(np.searchsorted(bts.values, np.datetime64(pd.Timestamp(s['roll_ts']).tz_localize(None))))
        if 0 < k < len(raw):
            g_raw = raw[k-1]; g_adj = adj[k-1]
            res.append(0.0 if abs(g_raw) < 1e-6 else abs(g_adj)/abs(g_raw))
    res = np.array(res)
    return dict(sym=sym, n_roll=len(res), cancel_med=(np.median(res)*100 if len(res) else 0),
                cancel_min=(np.min(res)*100 if len(res) else 0),
                neg=int((out['low'].astype(float)<=0).sum()),
                price_shift=float(segs['price_shift'].iloc[0]))

if __name__ == "__main__":
    syms = variety_syms()
    print(f"本地 roll_segment(min15) 品种数: {len(syms)}\n")
    rows = []
    for s in syms:
        r = strict_cancel(s)
        if r: rows.append(r)
    D = pd.DataFrame(rows)
    print("=== 用 roll_ts 精确定位的换月点，真实 back_adjust 抵消率 ===")
    print(f"  参与品种 {len(D)} | 换月点总数 {int(D.n_roll.sum())}")
    print(f"  抵消率中位 {D.cancel_med.median():.2f}% | 最小 {D.cancel_min.min():.2f}% | "
          f"未完全抵消(>1%)的品种 {int((D.cancel_min>1).sum())}")
    bad = D[D.cancel_min > 1]
    if len(bad):
        print("  未完全抵消的品种："); print(bad.to_string(index=False))
    print(f"\n=== 负价（真实 check_negative 口径：low+offset<=0）===")
    print(f"  有负价的品种 {int((D.neg>0).sum())} 个：{list(D[D.neg>0]['sym'])}")
    print(f"\n=== price_shift（--positivity 是否启用）===")
    n_pos = int((D.price_shift != 0).sum())
    print(f"  启用 positivity 的品种 {n_pos}/{len(D)} | 未启用 {len(D)-n_pos}")
    if len(D) - n_pos:
        print(f"  未启用列表（可能负价）：{list(D[D.price_shift==0]['sym'])[:30]}")
