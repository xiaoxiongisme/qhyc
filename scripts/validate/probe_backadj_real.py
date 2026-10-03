# -*- coding: utf-8 -*-
"""用【真实】back_adjust.apply_back_adjust 复验后复权是否抵消换月 gap。
只读本地库，不改任何数据。目的：确认 R12 缺陷是否真实、并定位根因。
"""
import os, sys
sys.path.insert(0, r"E:\Docker\qhyc")
os.chdir(r"E:\Docker\qhyc")

import numpy as np
import pandas as pd
import psycopg2

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")

# —— 1) 列类型与时区口径 ——
def col_types():
    cn = psycopg2.connect(**PG)
    cur = cn.cursor()
    cur.execute("""
        SELECT 'bar_15m.bucket' t, data_type, datetime_precision
        FROM information_schema.columns WHERE table_name='bar_15m' AND column_name='bucket'
        UNION ALL
        SELECT 'roll_segment.seg_start', data_type, datetime_precision
        FROM information_schema.columns WHERE table_name='roll_segment' AND column_name='seg_start'
    """)
    rows = cur.fetchall()
    cn.close()
    for r in rows:
        print(f"  {r[0]:<24} type={r[1]:<14} prec={r[2]}")

# —— 2) 取某品种真实 bar + 套真实 back_adjust ——
def real_apply(sym):
    from app.data.back_adjust import apply_back_adjust
    from sqlalchemy import create_engine, text
    eng = create_engine(f"postgresql+psycopg2://futures:qhyc_dev_pwd_2026@127.0.0.1:5432/futures")
    with eng.connect() as c:
        df = pd.read_sql(text(
            "SELECT bucket,open,high,low,close FROM bar_15m "
            "WHERE symbol=:s ORDER BY bucket"), c, params={"s": sym})
        segs = pd.read_sql(text(
            "SELECT seg_no,seg_start,seg_end,roll_ts,roll_delta,cum_offset,price_shift "
            "FROM roll_segment WHERE symbol=:s AND freq='min15' ORDER BY seg_no"),
            c, params={"s": sym})
    if df.empty or segs.empty:
        return None
    # 真实套用
    out = apply_back_adjust(df.copy(), sym, "min15", session=None,
                            segs=segs, time_col="bucket")
    return df, segs, out

# —— 3) 真实换月 gap 抵消率 ——
def gap_cancel(sym):
    r = real_apply(sym)
    if r is None:
        print(f"  {sym}: 无数据/无段，跳过"); return
    df, segs, out = r
    # 原始相邻 close->open 跳空（含换月处的大跳）
    raw_gap = (df['open'].astype(float).values[1:] - df['close'].astype(float).values[:-1])
    adj_close = out['close'].astype(float).values
    adj_open  = out['open'].astype(float).values
    adj_gap = adj_open[1:] - adj_close[:-1]
    # 换月处（|raw_gap| 大的）是否抵消
    big = np.abs(raw_gap) > 30  # 阈值，换月跳通常远大于日常
    if big.sum() == 0:
        print(f"  {sym}: 未检测到明显换月跳（n_seg={len(segs)}）"); return
    resid = np.abs(adj_gap[big]) / np.where(np.abs(raw_gap[big])>0, np.abs(raw_gap[big]), np.nan)
    resid = resid[np.isfinite(resid)]
    print(f"  {sym}: 换月跳 {big.sum()} 处 | 残留率中位 {np.nanmedian(resid)*100:.1f}% "
          f"| 最大 {np.nanmax(resid)*100:.1f}% | 负价根数 {(out['low'].astype(float)<=0).sum()}")
    # 打印前 3 个换月：原始 gap vs 复权后 gap
    idxs = np.where(big)[0][:3]
    for i in idxs:
        print(f"      bar#{i} raw_gap={raw_gap[i]:.1f}  adj_gap={adj_gap[i]:.1f}  "
              f"cum_offset_k={segs['cum_offset'].iloc[-1]:.1f}")

if __name__ == "__main__":
    print("=== 1) 列类型 / 时区口径 ===")
    col_types()
    print("\n=== 2) 真实 back_adjust 换月 gap 抵消对照（抽 5 个品种）===")
    for s in ["RB888", "FG888", "CU888", "TA888", "MA888"]:
        gap_cancel(s)
